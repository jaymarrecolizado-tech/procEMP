"""Procurement core: rules registry, append-only ledger, numbering, custody/human gates.

Zero external dependencies (stdlib only). SQLite dialect; see schema.sql.
This is build steps 1-3 of 01-revised-workflow-RA12009.md / 03-tech-spec-rules-evidence-gate.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
GENESIS_HASH = "0" * 64
ABSTAIN_THRESHOLD = 0.75

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
SEED_PATH = os.path.join(HERE, "seed_rules.json")
SEED_FORMS_PATH = os.path.join(HERE, "seed_forms.json")
SEED_CORPUS_PATH = os.path.join(HERE, "seed_corpus.json")

STAGES = [
    "DRAFT", "PLANNING", "BIDDING_DOCS", "ADVERTISEMENT", "RECEIVING",
    "OPENING_SCHEDULED", "CONTENT_INGESTION", "EVALUATION", "AWARD",
    "CONTRACT", "IMPLEMENTATION", "COMPLETION", "ARCHIVED",
]

DEFAULT_GATES = [
    ("OPENING_SCHEDULED", "CONTENT_INGESTION", "BAC_CHAIR", 1),
    ("CONTENT_INGESTION", "EVALUATION", "BAC_CHAIR", 1),
    ("EVALUATION", "AWARD", "BAC_CHAIR", 1),
    ("EVALUATION", "AWARD", "HOPE", 1),
    ("AWARD", "CONTRACT", "HOPE", 1),
    ("IMPLEMENTATION", "COMPLETION", "INSPECTION_COMMITTEE", 1),
    ("COMPLETION", "ARCHIVED", "BAC_SECRETARIAT", 1),
]


class CoreError(Exception):
    pass


class RuleResolutionError(CoreError):
    pass


class UnverifiedRuleError(CoreError):
    pass


class PolicyBlocked(CoreError):
    pass


class GateBlocked(CoreError):
    pass


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def uid() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str = ":memory:", seed: bool = True) -> sqlite3.Connection:
    conn = _connect(path)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    if seed:
        load_seed(conn)
        load_forms(conn)
        load_corpus(conn)
    return conn


def load_seed(conn: sqlite3.Connection, path: str = SEED_PATH) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    ts = now()
    conn.execute(
        "INSERT OR REPLACE INTO transition_policy (id, status, note) VALUES (1, ?, ?)",
        (data["transition_policy"]["status"], data["transition_policy"]["note"]),
    )
    for snap in data["corpus_snapshots"]:
        conn.execute(
            "INSERT OR REPLACE INTO corpus_snapshot "
            "(id, version, regime, created_at, checksum, approved_by, is_active) "
            "VALUES (?,?,?,?,?,?,?)",
            (snap["id"], snap["version"], snap["regime"], ts, snap["checksum"],
             snap["approved_by"], snap["is_active"]),
        )
    for rs in data["rulesets"]:
        conn.execute(
            "INSERT OR REPLACE INTO ruleset "
            "(id, regime, version, effective_from, effective_to, issuance_ref, "
            " checksum, approved_by, approved_at, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rs["id"], rs["regime"], rs["version"], rs["effective_from"],
             rs["effective_to"], rs["issuance_ref"], rs["checksum"],
             rs["approved_by"], ts, rs.get("note")),
        )
    for rule in data["rules"]:
        conn.execute(
            "INSERT OR REPLACE INTO rule "
            "(id, ruleset_id, key, value_json, section_ref, mode, verified, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid(), rule["ruleset_id"], rule["key"], canonical(rule["value"]),
             rule["section_ref"], rule.get("mode", "*"), int(rule.get("verified", False)),
             rule.get("note")),
        )


def load_forms(conn: sqlite3.Connection, path: str = SEED_FORMS_PATH) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for form in data["forms"]:
        conn.execute(
            "INSERT OR REPLACE INTO form_registry "
            "(code, name, classification, category, applies_to, required_fields, "
            " official_file_hash, official_file_version, body_available, verified, source, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (form["code"], form["name"], form["classification"], form["category"],
             canonical(form["applies_to"]), canonical(form["required_fields"]),
             form.get("official_file_hash"), form.get("official_file_version"),
             int(form.get("body_available", False)), int(form.get("verified", False)),
             data["source"], form.get("note")),
        )


def load_corpus(conn: sqlite3.Connection, path: str = SEED_CORPUS_PATH) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for chunk in data["chunks"]:
        if not chunk.get("source_url"):
            raise CoreError(f"chunk {chunk['chunk_id']} has no source_url")
        conn.execute(
            "INSERT OR REPLACE INTO corpus_chunk "
            "(chunk_id, corpus_snapshot_id, law, issuance_no, section, regime, "
            " effective_from, effective_to, supersedes, source_url, text, "
            " passage_hash, verified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chunk["chunk_id"], chunk.get("corpus_snapshot_id", data["corpus_snapshot_id"]),
             chunk["law"],
             chunk["issuance_no"], chunk["section"], chunk["regime"],
             chunk["effective_from"], chunk["effective_to"], chunk["supersedes"],
             chunk["source_url"], chunk["text"], sha256_text(chunk["text"]),
             int(chunk.get("verified", False))),
        )
    active = [c["chunk_id"] for c in data["chunks"] if c.get("verified")]
    checksum = sha256_text(canonical(sorted(active)))
    conn.execute(
        "UPDATE corpus_snapshot SET checksum=?, approved_by=? WHERE id=?",
        (checksum, "SEED_VERIFIED_OFFICIAL_TEXTS_REQUIRE_NAMED_HUMAN_REAPPROVAL",
         data["corpus_snapshot_id"]),
    )


# ---------------------------------------------------------------- ledger

def append_event(conn, tx_id, event_type, actor_id, actor_role, payload,
                 event_id=None) -> dict:
    event_id = event_id or uid()
    existing = conn.execute(
        "SELECT * FROM ledger_event WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing:
        return dict(existing)
    payload_text = canonical(payload)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # The chain head MUST be read inside the write transaction. Reading it
        # earlier lets two concurrent writers both chain off the same row and
        # fork the ledger, which silently destroys the COA audit trail.
        prev = conn.execute(
            "SELECT seq, row_hash FROM ledger_event ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["row_hash"] if prev else GENESIS_HASH
        occurred_at = now()
        row_hash = sha256_text("|".join(
            [prev_hash, event_id, occurred_at, actor_id, payload_text]))
        cur = conn.execute(
            "INSERT INTO ledger_event "
            "(event_id, tx_id, event_type, actor_id, actor_role, payload, "
            " occurred_at, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, tx_id, event_type, actor_id, actor_role, payload_text,
             occurred_at, prev_hash, row_hash),
        )
        seq = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"seq": seq, "event_id": event_id, "event_type": event_type,
            "prev_hash": prev_hash, "row_hash": row_hash}


def verify_chain(conn, from_seq=1, to_seq=None) -> dict:
    rows = conn.execute(
        "SELECT * FROM ledger_event WHERE seq >= ? AND (? IS NULL OR seq <= ?) ORDER BY seq",
        (from_seq, to_seq, to_seq),
    ).fetchall()
    expected_prev = GENESIS_HASH
    if from_seq > 1:
        anchor = conn.execute(
            "SELECT row_hash FROM ledger_event WHERE seq = ?", (from_seq - 1,)
        ).fetchone()
        expected_prev = anchor["row_hash"] if anchor else GENESIS_HASH
    errors = []
    last_seq = None
    for row in rows:
        if last_seq is not None and row["seq"] != last_seq + 1:
            errors.append({"seq": row["seq"], "problem": "sequence gap"})
        if row["prev_hash"] != expected_prev:
            errors.append({"seq": row["seq"], "problem": "prev_hash mismatch"})
        recomputed = sha256_text("|".join([
            row["prev_hash"], row["event_id"], row["occurred_at"],
            row["actor_id"], row["payload"],
        ]))
        if recomputed != row["row_hash"]:
            errors.append({"seq": row["seq"], "problem": "row_hash mismatch"})
        expected_prev = row["row_hash"]
        last_seq = row["seq"]
    return {"ok": not errors, "events_checked": len(rows),
            "first_seq": rows[0]["seq"] if rows else None,
            "last_seq": last_seq, "head_hash": expected_prev, "errors": errors}


def has_event(conn, tx_id, event_type) -> bool:
    return conn.execute(
        "SELECT 1 FROM ledger_event WHERE tx_id = ? AND event_type = ? LIMIT 1",
        (tx_id, event_type),
    ).fetchone() is not None


# ---------------------------------------------------------------- rules

def suggest_regime(conn, as_of) -> dict:
    """Fails closed until GPPB transition guidance is configured."""
    row = conn.execute("SELECT status, note FROM transition_policy WHERE id = 1").fetchone()
    if row and row["status"] == "CONFIRMED":
        return {"regime": row["note"], "source": "transition_policy"}
    return {"regime": "AMBIGUOUS", "reason": row["note"] if row else "no policy",
            "action": "legal officer must choose the regime explicitly"}


def resolve_rules(conn, as_of, regime) -> dict:
    as_of = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
    rs = conn.execute(
        "SELECT * FROM ruleset WHERE regime = ? AND effective_from <= ? "
        "AND (effective_to IS NULL OR ? < effective_to) "
        "ORDER BY effective_from DESC LIMIT 1",
        (regime, as_of, as_of),
    ).fetchone()
    if not rs:
        raise RuleResolutionError(f"no active ruleset for regime={regime} as_of={as_of}")
    snap = conn.execute(
        "SELECT * FROM corpus_snapshot WHERE regime = ? AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
        (regime,),
    ).fetchone()
    if not snap:
        raise RuleResolutionError(f"no active corpus snapshot for regime={regime}")
    rules = {}
    for r in conn.execute(
        "SELECT * FROM rule WHERE ruleset_id = ? AND mode = '*'", (rs["id"],)
    ).fetchall():
        rules[r["key"]] = {"value": json.loads(r["value_json"]),
                           "section_ref": r["section_ref"],
                           "verified": bool(r["verified"]), "note": r["note"]}
    return {"regime": regime, "ruleset_id": rs["id"],
            "ruleset_version": rs["version"], "corpus_snapshot_id": snap["id"],
            "corpus_version": snap["version"], "rules": rules}


def get_rule(conn, resolved, key, allow_unverified=False):
    entry = resolved["rules"].get(key)
    if entry is None:
        raise RuleResolutionError(f"rule '{key}' not defined for {resolved['ruleset_version']}")
    if not entry["verified"] and not allow_unverified:
        raise UnverifiedRuleError(
            f"rule '{key}' for {resolved['regime']} is unverified; "
            f"confirm with BAC Secretariat before use ({entry['section_ref']})"
        )
    return entry["value"]


def _normalize_posting_date(value):
    """Require a real ISO date; a typo must not resolve a ruleset silently."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise CoreError(
            f"posting_date must be an ISO date (YYYY-MM-DD), got {value!r}"
        ) from exc


def _validate_tx_context(resolved, mode, award_criterion):
    """Fail closed when mode/award criterion are outside the verified rules.

    Unverified rulesets (e.g. legacy RA 9184 placeholders) are not validated
    here: their rules fail closed on read, and the transaction records the
    legal officer's explicit regime choice. For verified rulesets the mode and
    award criterion must match exactly — never a silent best guess.
    """
    if not mode or not isinstance(mode, str):
        raise CoreError("mode is required (e.g. 'Competitive Bidding')")
    modes = resolved["rules"].get("MODES")
    if modes and modes["verified"] and mode not in modes["value"]:
        raise CoreError(
            f"mode '{mode}' is not a verified mode for {resolved['ruleset_version']} "
            f"({modes['section_ref']}); allowed: {', '.join(modes['value'])}"
        )
    if award_criterion:
        criteria = resolved["rules"].get("AWARD_CRITERIA")
        if criteria and criteria["verified"] and award_criterion not in criteria["value"]:
            raise CoreError(
                f"award_criterion '{award_criterion}' is not verified for "
                f"{resolved['ruleset_version']} ({criteria['section_ref']}); "
                f"allowed: {', '.join(criteria['value'])}"
            )


# ---------------------------------------------------------------- numbering

_issue_lock = threading.Lock()


def issue_number(conn, doc_type, unit, year) -> str:
    """Atomic, gap-free, logged. Safe across concurrent connections."""
    with _issue_lock:
        for _ in range(50):
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT next_value FROM number_sequence WHERE type=? AND unit=? AND year=?",
                    (doc_type, unit, year),
                ).fetchone()
                ordinal = row["next_value"] if row else 1
                if row:
                    conn.execute(
                        "UPDATE number_sequence SET next_value=? WHERE type=? AND unit=? AND year=?",
                        (ordinal + 1, doc_type, unit, year),
                    )
                else:
                    conn.execute(
                        "INSERT INTO number_sequence (type, unit, year, next_value) VALUES (?,?,?,?)",
                        (doc_type, unit, year, ordinal + 1),
                    )
                number = f"{unit}-{doc_type}-{year}-{ordinal:05d}"
                conn.execute(
                    "INSERT INTO issued_number (number, type, unit, year, ordinal, status, issued_at) "
                    "VALUES (?,?,?,?,?,'ACTIVE',?)",
                    (number, doc_type, unit, year, ordinal, now()),
                )
                conn.execute("COMMIT")
                return number
            except sqlite3.OperationalError as exc:
                conn.execute("ROLLBACK")
                if "locked" not in str(exc).lower():
                    raise
        raise CoreError("could not obtain numbering lock")


def void_number(conn, number, reason, approver) -> None:
    row = conn.execute("SELECT * FROM issued_number WHERE number = ?", (number,)).fetchone()
    if not row:
        raise CoreError(f"unknown number {number}")
    if row["status"] == "VOID":
        return
    conn.execute(
        "UPDATE issued_number SET status='VOID', void_reason=?, void_approver=? WHERE number=?",
        (reason, approver, number),
    )
    append_event(conn, "SYSTEM", "NUMBER_VOIDED", approver, "APPROVER",
                 {"number": number, "reason": reason})


# ---------------------------------------------------------------- transactions

def create_transaction(conn, *, regime, posting_date, mode, award_criterion=None,
                       drafter_id, legal_basis_note, number=None, stage="RECEIVING") -> str:
    posting_date = _normalize_posting_date(posting_date)
    if not drafter_id:
        raise CoreError("drafter_id is required: every draft must have a named human author")
    if not legal_basis_note:
        raise CoreError("legal_basis_note is required: a human must state why this regime applies")
    if stage not in STAGES:
        raise CoreError(f"unknown stage '{stage}' (valid: {', '.join(STAGES)})")
    resolved = resolve_rules(conn, posting_date, regime)
    _validate_tx_context(resolved, mode, award_criterion)
    tx_id = uid()
    number = number or issue_number(conn, "PR", "RO2", int(posting_date[:4]))
    conn.execute(
        "INSERT INTO proc_tx (id, number, regime, ruleset_id, ruleset_version, "
        "corpus_snapshot_id, mode, award_criterion, posting_date, stage, drafter_id, "
        "legal_basis_note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tx_id, number, regime, resolved["ruleset_id"], resolved["ruleset_version"],
         resolved["corpus_snapshot_id"], mode, award_criterion, posting_date, stage,
         drafter_id, legal_basis_note, now()),
    )
    for from_s, to_s, role, count in DEFAULT_GATES:
        conn.execute(
            "INSERT INTO stage_gate (id, tx_id, from_stage, to_stage, required_role, required_count) "
            "VALUES (?,?,?,?,?,?)",
            (uid(), tx_id, from_s, to_s, role, count),
        )
    append_event(conn, tx_id, "TRANSACTION_CREATED", drafter_id, "BAC_SECRETARIAT", {
        "number": number, "regime": regime, "ruleset_version": resolved["ruleset_version"],
        "corpus_snapshot_id": resolved["corpus_snapshot_id"], "mode": mode,
        "award_criterion": award_criterion, "posting_date": posting_date,
        "legal_basis_note": legal_basis_note,
    })
    return tx_id


def get_tx(conn, tx_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM proc_tx WHERE id = ?", (tx_id,)).fetchone()
    if not row:
        raise CoreError(f"unknown transaction {tx_id}")
    return row


# ---------------------------------------------------------------- custody gate

def ingest_document(conn, tx_id, artifact_class, content, metadata, actor_id="system",
                    actor_role="SERVICE") -> dict:
    """I3: no bid content is stored before the public opening event exists."""
    tx = get_tx(conn, tx_id)
    metadata = dict(metadata or {})
    if any(k in metadata for k in ("password", "bid_password", "passphrase")):
        raise PolicyBlocked("e-bid passwords must never enter the system (IRR Sec. 58.3)")
    if artifact_class in ("BID_CONTENT", "FINANCIAL_ENVELOPE", "TECHNICAL_ENVELOPE"):
        if not has_event(conn, tx_id, "BID_OPENING_RECORDED"):
            append_event(conn, tx_id, "POLICY_BLOCKED", actor_id, actor_role, {
                "attempt": "ingest_document", "artifact_class": artifact_class,
                "reason": "no BID_OPENING_RECORDED event",
            })
            raise PolicyBlocked(
                "bid contents are sealed until the BAC publicly opens them "
                "(IRR RA 12009 Sec. 58.1, 58.5, 59.1)"
            )
    if artifact_class == "FINANCIAL_ENVELOPE":
        resolved = resolve_rules(conn, tx["posting_date"], tx["regime"])
        sealed_for = get_rule(conn, resolved, "FINANCIAL_ENVELOPE_SEALED_FOR",
                              allow_unverified=True) if "FINANCIAL_ENVELOPE_SEALED_FOR" in resolved["rules"] else []
        if tx["award_criterion"] in sealed_for and not has_event(conn, tx_id, "FINANCIAL_OPENING_RECORDED"):
            append_event(conn, tx_id, "POLICY_BLOCKED", actor_id, actor_role, {
                "attempt": "ingest_financial_envelope",
                "reason": "financial envelopes sealed until second opening (Sec. 58.2(b))",
            })
            raise PolicyBlocked("financial envelope sealed for MEARB/MARB (IRR Sec. 58.2(b))")
    content_hash = sha256_text(content if isinstance(content, str) else repr(content))
    doc_id = uid()
    opening = conn.execute(
        "SELECT seq FROM ledger_event WHERE tx_id=? AND event_type='BID_OPENING_RECORDED' ORDER BY seq DESC LIMIT 1",
        (tx_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO document (id, tx_id, artifact_class, content_hash, metadata, ingested_at, opening_seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc_id, tx_id, artifact_class, content_hash, canonical(metadata), now(),
         opening["seq"] if opening else None),
    )
    append_event(conn, tx_id, "DOCUMENT_INGESTED", actor_id, actor_role, {
        "document_id": doc_id, "artifact_class": artifact_class,
        "content_hash": content_hash, "opening_seq": opening["seq"] if opening else None,
    })
    return {"document_id": doc_id, "content_hash": content_hash}


def record_bid_opening(conn, tx_id, chair_id, minutes_ref, envelopes, attendees) -> dict:
    return append_event(conn, tx_id, "BID_OPENING_RECORDED", chair_id, "BAC_CHAIR", {
        "minutes_ref": minutes_ref, "envelopes_opened": envelopes,
        "financial_envelopes_sealed": "FINANCIAL_ENVELOPE" not in envelopes,
        "attendees": attendees, "password_disclosed_by_bidders": True,
        "recorded_at": now(),
    })


def record_financial_opening(conn, tx_id, chair_id, minutes_ref) -> dict:
    return append_event(conn, tx_id, "FINANCIAL_OPENING_RECORDED", chair_id, "BAC_CHAIR", {
        "minutes_ref": minutes_ref, "recorded_at": now(),
    })


# ---------------------------------------------------------------- human gates

def add_signoff(conn, tx_id, from_stage, to_stage, signer_id, signer_role,
                document_hash, method="WET_INK_SCAN", signature_hash=None) -> str:
    gate = conn.execute(
        "SELECT * FROM stage_gate WHERE tx_id=? AND from_stage=? AND to_stage=? AND required_role=?",
        (tx_id, from_stage, to_stage, signer_role),
    ).fetchone()
    if not gate:
        raise CoreError(f"no gate configured for {from_stage}->{to_stage} / {signer_role}")
    sid = uid()
    conn.execute(
        "INSERT INTO signoff (id, tx_id, gate_id, signer_id, signer_role, method, "
        "signature_hash, document_hash, signed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, tx_id, gate["id"], signer_id, signer_role, method,
         signature_hash or sha256_text(f"{signer_id}|{document_hash}|{now()}"),
         document_hash, now()),
    )
    append_event(conn, tx_id, "SIGNOFF_RECORDED", signer_id, signer_role, {
        "signoff_id": sid, "gate_id": gate["id"], "from_stage": from_stage,
        "to_stage": to_stage, "document_hash": document_hash, "method": method,
    })
    return sid


def _valid_signoffs(conn, tx_id, gate, document_hash, drafter_id):
    return conn.execute(
        "SELECT DISTINCT signer_id FROM signoff WHERE gate_id=? AND document_hash=? "
        "AND signer_role=? AND signer_id<>?",
        (gate["id"], document_hash, gate["required_role"], drafter_id),
    ).fetchall()


def request_transition(conn, tx_id, to_stage, current_document_hash) -> dict:
    tx = get_tx(conn, tx_id)
    from_stage = tx["stage"]
    if to_stage not in STAGES:
        raise CoreError(f"unknown stage {to_stage}")
    if to_stage == "CONTENT_INGESTION" and not has_event(conn, tx_id, "BID_OPENING_RECORDED"):
        append_event(conn, tx_id, "GATE_BLOCKED", "system", "STATE_MACHINE", {
            "from_stage": from_stage, "to_stage": to_stage,
            "reason": "BID_OPENING_RECORDED event missing",
        })
        raise GateBlocked("public bid opening must be recorded before content ingestion")
    latest = conn.execute(
        "SELECT content_hash FROM draft WHERE tx_id=? ORDER BY created_at DESC LIMIT 1",
        (tx_id,),
    ).fetchone()
    if latest and latest["content_hash"] != current_document_hash:
        append_event(conn, tx_id, "GATE_BLOCKED", "system", "STATE_MACHINE", {
            "from_stage": from_stage, "to_stage": to_stage,
            "reason": "stale document hash: draft was edited after the signoff",
            "supplied": current_document_hash, "current": latest["content_hash"],
        })
        raise GateBlocked("document was edited; signoffs bound to the old hash are invalid")
    gates = conn.execute(
        "SELECT * FROM stage_gate WHERE tx_id=? AND from_stage=? AND to_stage=?",
        (tx_id, from_stage, to_stage),
    ).fetchall()
    missing = []
    for gate in gates:
        found = _valid_signoffs(conn, tx_id, gate, current_document_hash, tx["drafter_id"])
        if len(found) < gate["required_count"]:
            missing.append({"role": gate["required_role"],
                            "needed": gate["required_count"], "have": len(found),
                            "reasons": ["no signoff for this document hash",
                                        "or signer is also the drafter"]})
    if missing:
        append_event(conn, tx_id, "GATE_BLOCKED", "system", "STATE_MACHINE", {
            "from_stage": from_stage, "to_stage": to_stage, "missing": missing,
            "document_hash": current_document_hash,
        })
        raise GateBlocked(f"transition {from_stage}->{to_stage} blocked: {canonical(missing)}")
    conn.execute("UPDATE proc_tx SET stage=? WHERE id=?", (to_stage, tx_id))
    return append_event(conn, tx_id, "STAGE_TRANSITION", "system", "STATE_MACHINE", {
        "from_stage": from_stage, "to_stage": to_stage,
        "document_hash": current_document_hash,
        "satisfied_gates": [g["required_role"] for g in gates],
    })


# ---------------------------------------------------------------- verdicts

def submit_verdict(conn, tx_id, verdict, actor_id="checker", actor_role="MODEL") -> dict:
    required = ["status", "missing_documents", "invalid_documents", "notes",
                "evidence_spans", "rule_citations", "confidence", "decision",
                "model_version", "ruleset_version", "corpus_snapshot_id", "prompt_hash"]
    missing = [k for k in required if k not in verdict]
    if missing:
        raise CoreError(f"verdict missing fields: {missing}")
    if verdict["decision"] not in ("PASS", "FAIL", "ABSTAIN"):
        raise CoreError("invalid decision")
    reasons = []
    if verdict["confidence"] < ABSTAIN_THRESHOLD:
        reasons.append("confidence below threshold")
    if not verdict["rule_citations"] or not verdict["evidence_spans"]:
        reasons.append("missing citations or evidence spans")
    if not all(c.get("verified", True) for c in verdict["rule_citations"]):
        reasons.append("unverified citation")
    if reasons and verdict["decision"] != "ABSTAIN":
        verdict = dict(verdict)
        verdict["decision"] = "ABSTAIN"
        verdict["abstain_reasons"] = reasons
    verdict = dict(verdict)
    verdict["advisory"] = True
    vid = uid()
    conn.execute(
        "INSERT INTO verdict (id, tx_id, payload, created_at) VALUES (?,?,?,?)",
        (vid, tx_id, canonical(verdict), now()),
    )
    append_event(conn, tx_id, "VERDICT_PRODUCED", actor_id, actor_role, {
        "verdict_id": vid, "decision": verdict["decision"],
        "model_version": verdict["model_version"],
        "ruleset_version": verdict["ruleset_version"],
        "corpus_snapshot_id": verdict["corpus_snapshot_id"],
        "prompt_hash": verdict["prompt_hash"],
    })
    verdict["verdict_id"] = vid
    if verdict["decision"] in ("ABSTAIN", "FAIL"):
        try:
            from review_queue import enqueue_verdict
            enqueue_verdict(conn, tx_id, vid, created_by=actor_id)
        except Exception:
            # Review-queue enqueueing is advisory automation; verdict submission
            # must still succeed.
            pass
    return verdict


def get_verdict(conn, verdict_id) -> dict:
    row = conn.execute("SELECT payload FROM verdict WHERE id=?", (verdict_id,)).fetchone()
    if not row:
        raise CoreError("unknown verdict")
    return json.loads(row["payload"])


# ---------------------------------------------------------------- audit

def export_folder(conn, tx_id) -> dict:
    tx = dict(get_tx(conn, tx_id))
    events = [dict(r) for r in conn.execute(
        "SELECT seq, event_id, event_type, actor_id, actor_role, payload, occurred_at, "
        "prev_hash, row_hash FROM ledger_event WHERE tx_id=? ORDER BY seq", (tx_id,)
    ).fetchall()]
    documents = [dict(r) for r in conn.execute(
        "SELECT * FROM document WHERE tx_id=? ORDER BY ingested_at", (tx_id,)
    ).fetchall()]
    signoffs = [dict(r) for r in conn.execute(
        "SELECT * FROM signoff WHERE tx_id=? ORDER BY signed_at", (tx_id,)
    ).fetchall()]
    verdicts = [dict(r) for r in conn.execute(
        "SELECT * FROM verdict WHERE tx_id=? ORDER BY created_at", (tx_id,)
    ).fetchall()]
    drafts = [dict(r) for r in conn.execute(
        "SELECT * FROM draft WHERE tx_id=? ORDER BY created_at", (tx_id,)
    ).fetchall()]
    body = {"transaction": tx, "events": events, "documents": documents,
            "signoffs": signoffs, "verdicts": verdicts, "drafts": drafts}
    chain = verify_chain(conn)
    return {**body, "manifest": {
        "folder_hash": sha256_text(canonical(body)),
        "ledger_head_hash": chain["head_hash"],
        "chain_ok": chain["ok"],
        "event_count": len(events),
        "exported_at": now(),
    }}


def migrate(source_conn, target_path, actor_id="it-admin") -> dict:
    """Local -> VPS: copy preserving seq/hashes, then verify the chain on the target."""
    if os.path.exists(target_path):
        os.remove(target_path)
    target = init_db(target_path, seed=False)
    with open(SEED_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    target.execute("INSERT INTO transition_policy (id,status,note) VALUES (1,?,?)",
                   (data["transition_policy"]["status"], data["transition_policy"]["note"]))
    for table in ("ruleset", "rule", "corpus_snapshot", "corpus_chunk", "form_registry", "proc_tx",
                  "stage_gate", "signoff", "number_sequence", "issued_number",
                  "document", "verdict", "draft", "sealed_folder",
                  "contract_terms", "milestone", "variation_order",
                  "correction", "citation_feedback", "benchmark_case",
                  "training_batch", "model_version", "eval_run",
                  "review_item", "review_action"):
        cols = [r[1] for r in source_conn.execute(f"PRAGMA table_info({table})").fetchall()]
        collist = ",".join(cols)
        rows = source_conn.execute(f"SELECT {collist} FROM {table}").fetchall()
        if rows:
            placeholders = ",".join("?" for _ in cols)
            target.executemany(
                f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
                [tuple(r[c] for c in cols) for r in rows],
            )
    rows = source_conn.execute(
        "SELECT seq,event_id,tx_id,event_type,actor_id,actor_role,payload,occurred_at,prev_hash,row_hash "
        "FROM ledger_event ORDER BY seq"
    ).fetchall()
    target.executemany(
        "INSERT INTO ledger_event (seq,event_id,tx_id,event_type,actor_id,actor_role,payload,"
        "occurred_at,prev_hash,row_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [tuple(r) for r in rows],
    )
    report = verify_chain(target)
    append_event(target, "SYSTEM", "MIGRATION_VERIFIED", actor_id, "IT", {
        "source_events": len(rows), "chain_ok": report["ok"],
        "head_hash": report["head_hash"], "target": os.path.basename(target_path),
    })
    target.close()
    return report
