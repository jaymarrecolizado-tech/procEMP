"""Deterministic document checker (build step 4).

The LLM is NOT in this path. Extraction produces structured fields; this module
applies the BAC-approved required-document checklist deterministically and emits
an evidence-backed, advisory verdict.

Key compliance nuance: "the document is absent" and "the extractor could not read
the field" are different. The first is FAIL, the second is ABSTAIN -> human.
An AI system must never turn an OCR failure into a bidder disqualification.
"""

from __future__ import annotations

from datetime import date

from core import (
    CoreError, get_tx, get_rule, resolve_rules, submit_verdict,
    append_event, sha256_text, now, canonical,
)

MODEL_VERSION = "rules-engine-1.0-no-llm"


def _as_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _field(doc, name):
    """Return (present, value). Missing field => abstain, never FAIL."""
    if not isinstance(doc, dict) or name not in doc:
        return False, None
    return True, doc[name]


def evaluate_submission(conn, tx_id, checklist, extracted, actor_id="rules-engine"):
    """checklist: BAC-approved per-transaction required-document list.
       extracted: structured fields from the OCR/extraction pipeline.
    """
    if not checklist.get("approved_by"):
        raise CoreError("checklist must carry approved_by (a named BAC Secretariat officer)")
    tx = get_tx(conn, tx_id)
    if checklist.get("ruleset_version") != tx["ruleset_version"]:
        raise CoreError("checklist ruleset_version does not match the pinned transaction ruleset")
    resolved = resolve_rules(conn, tx["posting_date"], tx["regime"])
    if get_rule(conn, resolved, "CHECKLIST_AUTHORITY") != "BAC_APPROVED_PER_TRANSACTION":
        raise CoreError("checklist authority is not configured for this regime")

    as_of = _as_date(extracted.get("as_of") or tx["posting_date"])
    documents = extracted.get("documents", {})
    missing, invalid, notes, spans, citations, abstain = [], [], [], [], [], []

    for item in checklist["documents"]:
        key = item["key"]
        ref = item.get("section_ref", "BAC-approved checklist / Instructions to Bidders")
        citations.append({"section": ref, "verified": True,
                          "source": "BAC_APPROVED_CHECKLIST"})
        if key not in documents:
            missing.append(key)
            notes.append(f"{key}: not submitted")
            continue
        doc = documents[key]
        if doc is None:
            abstain.append(f"{key}: extraction produced no data")
            continue
        if isinstance(doc, dict) and doc.get("status") == "UNREADABLE":
            abstain.append(f"{key}: document unreadable, cannot determine contents")
            continue
        for check in item.get("checks", [{"type": "present"}]):
            ctype = check["type"]
            if ctype == "present":
                ok, val = _field(doc, "present")
                if not ok:
                    abstain.append(f"{key}.present: field not extracted")
                elif not val:
                    missing.append(key)
                    notes.append(f"{key}: marked not present")
                else:
                    spans.append({"field": f"{key}.present", "value": True})
            elif ctype == "signed":
                ok, val = _field(doc, "signed")
                if not ok:
                    abstain.append(f"{key}.signed: field not extracted")
                elif not val:
                    invalid.append(key)
                    notes.append(f"{key}: signature missing")
                else:
                    spans.append({"field": f"{key}.signed", "value": True})
            elif ctype == "notarized":
                ok, val = _field(doc, "notarized")
                if not ok:
                    abstain.append(f"{key}.notarized: field not extracted")
                elif not val:
                    invalid.append(key)
                    notes.append(f"{key}: not notarized")
                else:
                    spans.append({"field": f"{key}.notarized", "value": True})
            elif ctype == "not_expired":
                ok, val = _field(doc, "valid_until")
                if not ok:
                    abstain.append(f"{key}.valid_until: field not extracted")
                    continue
                expiry = _as_date(val)
                if expiry is None:
                    abstain.append(f"{key}.valid_until: unparseable date {val!r}")
                elif as_of and expiry < as_of:
                    invalid.append(key)
                    notes.append(f"{key}: expired on {expiry.isoformat()}")
                    spans.append({"field": f"{key}.valid_until", "value": val})
                else:
                    spans.append({"field": f"{key}.valid_until", "value": val})
            elif ctype == "amount_lte_abc":
                ok, val = _field(doc, "amount")
                if not ok:
                    abstain.append(f"{key}.amount: field not extracted")
                    continue
                limit = check.get("limit")
                if limit is None:
                    abstain.append(f"{key}.amount: no ABC limit supplied")
                elif float(val) > float(limit):
                    invalid.append(key)
                    notes.append(f"{key}: amount {val} exceeds ABC {limit}")
                    spans.append({"field": f"{key}.amount", "value": val})
                else:
                    spans.append({"field": f"{key}.amount", "value": val})
            else:
                raise CoreError(f"unknown check type: {ctype}")

    if abstain:
        decision, confidence = "ABSTAIN", 0.0
        notes.append("Abstained due to unextracted fields, NOT due to a model probability")
    elif missing or invalid:
        decision, confidence = "FAIL", 1.0
    else:
        decision, confidence = "PASS", 1.0

    verdict = submit_verdict(conn, tx_id, {
        "status": decision, "decision": decision, "confidence": confidence,
        "missing_documents": sorted(set(missing)),
        "invalid_documents": sorted(set(invalid)),
        "notes": notes + [f"abstain_reasons: {canonical(abstain)}" if abstain else "deterministic evaluation"],
        "evidence_spans": spans, "rule_citations": citations,
        "model_version": MODEL_VERSION,
        "ruleset_version": tx["ruleset_version"],
        "corpus_snapshot_id": tx["corpus_snapshot_id"],
        "prompt_hash": sha256_text("no-prompt-deterministic"),
        "checklist_approved_by": checklist["approved_by"],
        "evaluated_at": now(),
    }, actor_id=actor_id, actor_role="RULES_ENGINE")
    return verdict


def sample_checklist(ruleset_version):
    """Illustrative only — a real checklist is generated in Stage 1 and signed by the BAC."""
    return {
        "approved_by": "BAC_SECRETARIAT_DEMO_ONLY",
        "ruleset_version": ruleset_version,
        "documents": [
            {"key": "philgeps_registration", "section_ref": "IRR RA 12009 Sec. 59.1 / ITB checklist",
             "checks": [{"type": "present"}, {"type": "not_expired"}]},
            {"key": "omnibus_sworn_statement", "section_ref": "IRR RA 12009 Sec. 59.1 / ITB checklist",
             "checks": [{"type": "present"}, {"type": "signed"}, {"type": "notarized"}]},
            {"key": "bid_securing_declaration", "section_ref": "IRR RA 12009 Sec. 59.1 / ITB checklist",
             "checks": [{"type": "present"}, {"type": "signed"}]},
            {"key": "financial_proposal", "section_ref": "IRR RA 12009 Sec. 59.1 / ITB checklist",
             "checks": [{"type": "present"}, {"type": "amount_lte_abc", "limit": 1000000}]},
        ],
    }
