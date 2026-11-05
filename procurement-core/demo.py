"""End-to-end demo of the procurement core: dual-regime rules, sealed-bid gate,
human gates, evidence-backed verdicts, audit folder, local->VPS migration.

Run:  python demo.py
"""

import os
import tempfile

from core import (
    PolicyBlocked, GateBlocked, UnverifiedRuleError,
    init_db, suggest_regime, resolve_rules, get_rule, create_transaction, get_tx,
    ingest_document, record_bid_opening, add_signoff, request_transition,
    submit_verdict, export_folder, verify_chain, migrate, sha256_text,
)


def main():
    conn = init_db(":memory:")

    print("1. Regime suggestion (before GPPB transition guidance)")
    print("  ", suggest_regime(conn, "2026-11-05")["regime"],
          "- legal officer must choose explicitly\n")

    r = resolve_rules(conn, "2026-11-05", "RA12009_IRR")
    print("2. Resolved rules for a 2026 transaction")
    print("   ruleset:", r["ruleset_version"], "| corpus:", r["corpus_version"])
    print("   SVP threshold:", get_rule(conn, r, "SVP_THRESHOLD"))
    print("   min quotations to proceed:", get_rule(conn, r, "SVP_MIN_QUOTATIONS_TO_PROCEED"))
    print("   award criteria:", get_rule(conn, r, "AWARD_CRITERIA"))
    try:
        resolve_rules(conn, "2023-06-01", "RA9184_2016IRR")
        legacy = resolve_rules(conn, "2023-06-01", "RA9184_2016IRR")
        get_rule(conn, legacy, "SVP_THRESHOLD")
    except UnverifiedRuleError as exc:
        print("   legacy rules:", str(exc)[:110], "... -> fail closed\n")

    tx = create_transaction(
        conn, regime="RA12009_IRR", posting_date="2026-11-05",
        mode="Small Value Procurement", award_criterion="LCRB",
        drafter_id="staff-01",
        legal_basis_note="Legal officer memo 2026-10-30: RA 12009 applies to new postings",
    )
    print("3. Transaction", get_tx(conn, tx)["number"], "stage:", get_tx(conn, tx)["stage"])

    print("4. Try to OCR a submitted bid BEFORE public opening")
    try:
        ingest_document(conn, tx, "BID_CONTENT", "sealed bid contents",
                        {"bidder": "Supplier A"})
        print("   ERROR: should have been blocked")
    except PolicyBlocked as exc:
        print("   BLOCKED:", str(exc)[:95], "...")

    request_transition(conn, tx, "OPENING_SCHEDULED", sha256_text("receipt-log-v1"))
    record_bid_opening(conn, tx, "bac-chair-01", "minutes-2026-11-05",
                       ["TECHNICAL_ENVELOPE"], ["bac-chair-01", "observer-01"])
    add_signoff(conn, tx, "OPENING_SCHEDULED", "CONTENT_INGESTION",
                "bac-chair-01", "BAC_CHAIR", sha256_text("minutes-2026-11-05"))
    request_transition(conn, tx, "CONTENT_INGESTION", sha256_text("minutes-2026-11-05"))

    doc = ingest_document(conn, tx, "BID_CONTENT", "bid contents after opening",
                          {"bidder": "Supplier A"})
    print("5. After the opening event: ingested", doc["content_hash"][:16], "...")

    print("6. Transition to EVALUATION without a signoff")
    try:
        request_transition(conn, tx, "EVALUATION", sha256_text("eval-draft-v1"))
        print("   ERROR: should have been blocked")
    except GateBlocked:
        print("   BLOCKED: missing BAC_CHAIR signoff")
    add_signoff(conn, tx, "CONTENT_INGESTION", "EVALUATION",
                "bac-chair-01", "BAC_CHAIR", sha256_text("eval-draft-v1"))
    request_transition(conn, tx, "EVALUATION", sha256_text("eval-draft-v1"))
    print("   after signoff: stage =", get_tx(conn, tx)["stage"], "\n")

    from copy import deepcopy
    from checker import evaluate_submission, sample_checklist

    checklist = sample_checklist(get_tx(conn, tx)["ruleset_version"])
    good = {"as_of": "2026-11-05", "documents": {
        "philgeps_registration": {"present": True, "valid_until": "2027-06-30"},
        "omnibus_sworn_statement": {"present": True, "signed": True, "notarized": True},
        "bid_securing_declaration": {"present": True, "signed": True},
        "financial_proposal": {"present": True, "amount": 950000},
    }}
    v1 = evaluate_submission(conn, tx, checklist, good)
    print("7. Checker (deterministic, no LLM):", v1["decision"],
          "| model:", v1["model_version"])
    expired = deepcopy(good)
    expired["documents"]["philgeps_registration"]["valid_until"] = "2025-01-01"
    v2 = evaluate_submission(conn, tx, checklist, expired)
    print("   expired PhilGEPS registration:", v2["decision"],
          "| invalid:", v2["invalid_documents"])
    unreadable = deepcopy(good)
    unreadable["documents"]["omnibus_sworn_statement"] = {"status": "UNREADABLE"}
    v3 = evaluate_submission(conn, tx, checklist, unreadable)
    print("   unreadable scan:", v3["decision"],
          "(routed to human, never a disqualification)")

    from forms import render_draft, list_forms
    noa = {
        "approved_by": "bac-secretariat-01",
        "values": {"awardee_name": "ACME Corp", "project_title": "Laptop supply",
                   "contract_amount": 950000, "hop_e_signature": "PENDING"},
        "source_refs": {"awardee_name": "BAC Resolution 2026-011",
                        "project_title": "Approved PR RO2-PR-2026-00001",
                        "contract_amount": "Abstract of Quotations 2026-011",
                        "hop_e_signature": "HOPE signature sheet"},
    }
    draft = render_draft(conn, tx, "NOTICE_OF_AWARD", noa)
    noa_class = [f["classification"] for f in list_forms(conn)
                 if f["code"] == "NOTICE_OF_AWARD"][0]
    print("8. Deterministic drafting:", draft["status"])
    print("   Notice of Award is a", noa_class, "form (NOT a Standard Form);",
          "official body not registered -> release blocked")

    folder = export_folder(conn, tx)
    print("9. Audit folder:", folder["manifest"]["event_count"], "events |",
          "chain ok:", folder["manifest"]["chain_ok"], "|",
          "folder hash:", folder["manifest"]["folder_hash"][:16], "...")

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "vps.db")
        report = migrate(conn, target, actor_id="it-admin-01")
        print("10. Local -> VPS migration:", report["events_checked"],
              "events copied | chain ok:", report["ok"])

    print("\nDone. No external AI/embedding API was called at any point.")


if __name__ == "__main__":
    main()
