"""Step 8: COA-ready audit packaging, WORM sealing, read-only verification.

A sealed folder is an immutable snapshot of the entire procurement lifecycle:
transaction header, every ledger event with its hashes, documents and checksums,
signoffs, verdicts, drafts, rule citations, corpus/model versions, the hash-chain
state it attests to, and a meta-completeness check across the whole lifecycle.

WORM semantics: sealed rows cannot be updated or deleted (DB triggers). If the
underlying transaction changes, a NEW numbered version must be created with a
recorded reason; the old version stays for audit.

The COA verification path is read-only — it recomputes hashes, never writes.
"""

from __future__ import annotations

import json
import os

from core import (
    CoreError, STAGES, append_event, canonical, export_folder, get_tx, now,
    sha256_text, uid, verify_chain,
)
from rag import verify_corpus_integrity

FOLDER_SCHEMA_VERSION = "1.0"


def _event_types(folder):
    return {e["event_type"] for e in folder["events"]}


def meta_completeness(folder):
    """Structural completeness flags across the lifecycle — advisory only.
    A flag means 'check this', NOT a legal finding of non-compliance."""
    tx = folder["transaction"]
    stage_index = STAGES.index(tx["stage"]) if tx["stage"] in STAGES else 0
    types = _event_types(folder)
    checks = []

    def expect(min_stage, item, present, evidence=""):
        if stage_index >= STAGES.index(min_stage):
            checks.append({"item": item, "present": bool(present), "evidence": evidence})

    expect("RECEIVING", "transaction_created_event",
           "TRANSACTION_CREATED" in types)
    expect("ADVERTISEMENT", "advertisement_or_stage_transition",
           "STAGE_TRANSITION" in types, "stage transitions recorded in ledger")
    expect("CONTENT_INGESTION", "public_bid_opening_record",
           "BID_OPENING_RECORDED" in types, "IRR RA 12009 Sec. 58.1")
    expect("CONTENT_INGESTION", "bid_documents_ingested",
           any(d["artifact_class"] in ("BID_CONTENT", "TECHNICAL_ENVELOPE",
                                       "FINANCIAL_ENVELOPE")
               for d in folder["documents"]))
    expect("EVALUATION", "evaluation_verdict", len(folder["verdicts"]) > 0)
    expect("EVALUATION", "bac_chair_signoff",
           any(s["signer_role"] == "BAC_CHAIR" for s in folder["signoffs"]))
    expect("AWARD", "award_draft",
           any(d["form_code"] == "NOTICE_OF_AWARD" for d in folder["drafts"]))
    expect("AWARD", "hope_signoff",
           any(s["signer_role"] == "HOPE" for s in folder["signoffs"]))
    expect("CONTRACT", "contract_form_draft",
           any(d["form_code"] == "CONTRACT_FORM" for d in folder["drafts"]))
    expect("COMPLETION", "completion_documents",
           any(d["artifact_class"] == "COMPLETION_DOCUMENT"
               for d in folder["documents"]))
    flags = [c for c in checks if not c["present"]]
    return {"ok": not flags, "flags": flags, "checked": checks,
            "note": "structural check only; not a legal determination"}


def _regime_context(conn, tx):
    rs = conn.execute("SELECT * FROM ruleset WHERE id=?", (tx["ruleset_id"],)).fetchone()
    snap = conn.execute("SELECT * FROM corpus_snapshot WHERE id=?",
                        (tx["corpus_snapshot_id"],)).fetchone()
    policy = conn.execute("SELECT status FROM transition_policy WHERE id=1").fetchone()
    return {
        "regime": tx["regime"], "ruleset_version": tx["ruleset_version"],
        "ruleset_id": tx["ruleset_id"],
        "ruleset_issuance_ref": rs["issuance_ref"] if rs else None,
        "corpus_snapshot_id": tx["corpus_snapshot_id"],
        "corpus_version": snap["version"] if snap else None,
        "corpus_checksum": snap["checksum"] if snap else None,
        "corpus_approved_by": snap["approved_by"] if snap else None,
        "transition_policy_status": policy["status"] if policy else None,
        "posting_date": tx["posting_date"],
    }


def compile_folder(conn, tx_id, compiler_id="audit-compiler"):
    """Read-only compile. Does NOT seal or mutate anything."""
    base = export_folder(conn, tx_id)
    chain = verify_chain(conn)
    citations, chunk_ids, model_versions = [], set(), set()
    for row in base["verdicts"]:
        v = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        for c in v.get("rule_citations", []):
            citations.append(c)
            if c.get("chunk_id"):
                chunk_ids.add(c["chunk_id"])
        for key in ("model_version", "rules_engine_version"):
            if v.get(key):
                model_versions.add(v[key])

    folder = {
        "schema_version": FOLDER_SCHEMA_VERSION,
        "compiler": compiler_id,
        "transaction": base["transaction"],
        "regime_context": _regime_context(conn, base["transaction"]),
        "events": base["events"],
        "documents": base["documents"],
        "signoffs": base["signoffs"],
        "verdicts": base["verdicts"],
        "drafts": base["drafts"],
        "rule_citations": citations,
        "corpus_chunks_used": sorted(chunk_ids),
        "model_versions": sorted(model_versions),
        "ledger_chain": chain,
        "meta_completeness": None,
        "manifest": {},
    }
    folder["meta_completeness"] = meta_completeness(folder)
    body = {k: v for k, v in folder.items() if k != "manifest"}
    folder_hash = sha256_text(canonical(body))
    folder["manifest"] = {
        "folder_hash": folder_hash,
        "ledger_head_hash": chain["head_hash"],
        "attested_last_seq": chain["last_seq"],
        "events_checked": chain["events_checked"],
        "event_count": len(base["events"]),
        "document_count": len(base["documents"]),
        "signoff_count": len(base["signoffs"]),
        "verdict_count": len(base["verdicts"]),
        "draft_count": len(base["drafts"]),
        "citation_count": len(citations),
        "corpus_integrity": verify_corpus_integrity(conn)["ok"],
        "meta_completeness_ok": folder["meta_completeness"]["ok"],
        "compiled_at": now(),
    }
    return folder


def _latest_seal(conn, tx_id):
    return conn.execute(
        "SELECT * FROM sealed_folder WHERE tx_id=? ORDER BY version DESC LIMIT 1",
        (tx_id,),
    ).fetchone()


def list_seals(conn, tx_id=None):
    if tx_id:
        rows = conn.execute(
            "SELECT id, tx_id, version, folder_hash, sealed_at, sealed_by, "
            "supersedes_seal_id, supersede_reason FROM sealed_folder "
            "WHERE tx_id=? ORDER BY version", (tx_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, tx_id, version, folder_hash, sealed_at, sealed_by, "
            "supersedes_seal_id, supersede_reason FROM sealed_folder "
            "ORDER BY sealed_at, version"
        ).fetchall()
    return [dict(r) for r in rows]


def seal_folder(conn, tx_id, sealed_by, supersede_reason=None,
                compiler_id="audit-compiler"):
    """Create an immutable, hash-sealed COA folder."""
    if not sealed_by:
        raise CoreError("sealed_by (a named BAC Secretariat officer) is required")
    folder = compile_folder(conn, tx_id, compiler_id)
    folder_hash = folder["manifest"]["folder_hash"]
    existing = _latest_seal(conn, tx_id)
    if existing:
        # The FOLDER_SEALED event from the previous seal is bookkeeping, not a
        # change to the underlying transaction. Only real new events require a
        # superseding version.
        newer = conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE seq > ? "
            "AND event_type <> 'FOLDER_SEALED'",
            (existing["attested_last_seq"],),
        ).fetchone()["c"]
        if newer == 0:
            return dict(existing)
        if not supersede_reason:
            raise CoreError(
                "folder content changed after sealing; WORM policy requires a "
                "supersede_reason to create a new version (old version is retained)"
            )
        version = existing["version"] + 1
        supersedes = existing["id"]
    else:
        version, supersedes = 1, None

    seal_id = uid()
    append_event(conn, tx_id, "FOLDER_SEALED", sealed_by, "BAC_SECRETARIAT", {
        "seal_id": seal_id, "version": version, "folder_hash": folder_hash,
        "attested_chain_head": folder["manifest"]["ledger_head_hash"],
        "attested_last_seq": folder["manifest"]["attested_last_seq"],
        "supersedes_seal_id": supersedes,
        "supersede_reason": supersede_reason,
    })
    conn.execute(
        "INSERT INTO sealed_folder (id, tx_id, version, folder_hash, "
        "attested_chain_head, attested_last_seq, content_json, sealed_at, "
        "sealed_by, supersedes_seal_id, supersede_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (seal_id, tx_id, version, folder_hash,
         folder["manifest"]["ledger_head_hash"],
         folder["manifest"]["attested_last_seq"],
         canonical(folder), now(), sealed_by, supersedes, supersede_reason),
    )
    return {"id": seal_id, "tx_id": tx_id, "version": version,
            "folder_hash": folder_hash, "sealed_by": sealed_by,
            "supersedes_seal_id": supersedes, "supersede_reason": supersede_reason}


def verify_sealed_folder(conn, seal_id):
    """COA verification path. Pure reads — no ledger writes, no mutation."""
    row = conn.execute("SELECT * FROM sealed_folder WHERE id=?", (seal_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "unknown seal_id"}
    try:
        content = json.loads(row["content_json"])
    except json.JSONDecodeError:
        content = {}
    if not isinstance(content, dict):
        content = {}
    manifest = content.get("manifest") or {}
    body = {k: v for k, v in content.items() if k != "manifest"}
    recomputed = sha256_text(canonical(body))
    hash_ok = (recomputed == row["folder_hash"]
               == manifest.get("folder_hash"))
    chain = verify_chain(conn, 1, row["attested_last_seq"])
    chain_ok = chain["ok"]
    head_match = chain["head_hash"] == row["attested_chain_head"]
    corpus = verify_corpus_integrity(conn)
    return {
        "ok": hash_ok and chain_ok and head_match and corpus["ok"],
        "seal_id": seal_id, "version": row["version"],
        "sealed_by": row["sealed_by"], "sealed_at": row["sealed_at"],
        "folder_hash_ok": hash_ok,
        "recomputed_hash": recomputed,
        "stored_hash": row["folder_hash"],
        "chain_ok": chain_ok, "chain_head_matches": head_match,
        "attested_last_seq": row["attested_last_seq"],
        "corpus_ok": corpus["ok"],
        "meta_completeness_ok": manifest.get("meta_completeness_ok"),
        "detail": {"chain": chain, "corpus": corpus},
    }


def export_package(conn, seal_id, output_dir):
    """Write a self-contained COA package. Refuses to export a broken seal."""
    report = verify_sealed_folder(conn, seal_id)
    if not report["ok"]:
        raise CoreError(f"refusing to export an unverifiable folder: {report}")
    row = conn.execute("SELECT * FROM sealed_folder WHERE id=?", (seal_id,)).fetchone()
    os.makedirs(output_dir, exist_ok=True)
    package_path = os.path.join(output_dir, "package.json")
    manifest_path = os.path.join(output_dir, "MANIFEST.sha256")
    with open(package_path, "w", encoding="utf-8") as fh:
        json.dump(json.loads(row["content_json"]), fh, indent=2, ensure_ascii=False)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write(f"folder_hash  {row['folder_hash']}\n")
        with open(package_path, encoding="utf-8") as pkg:
            fh.write(f"package.json  {sha256_text(pkg.read())}\n")
        fh.write(f"seal_id  {seal_id}\n")
        fh.write(f"version  {row['version']}\n")
        fh.write(f"sealed_by  {row['sealed_by']}\n")
        fh.write(f"sealed_at  {row['sealed_at']}\n")
        fh.write(f"attested_chain_head  {row['attested_chain_head']}\n")
        fh.write(f"verification  {canonical({k: report[k] for k in ('ok','folder_hash_ok','chain_ok','chain_head_matches','corpus_ok')})}\n")
    return {"package": package_path, "manifest": manifest_path,
            "folder_hash": row["folder_hash"], "verification": report}
