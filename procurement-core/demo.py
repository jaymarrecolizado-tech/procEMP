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

    from extraction import StubBackend, extract_and_check, valid_stub_response
    from rag import AsOfRAG, verify_corpus_integrity
    rag = AsOfRAG(conn)
    pages = [{"page": 1, "text": "PhilGEPS Platinum, valid until 2027-06-30"},
             {"page": 2, "text": "Financial proposal: PHP 950,000.00"}]
    ev, er = extract_and_check(conn, tx, checklist, pages,
                               StubBackend(valid_stub_response()), rag=rag)
    print("8. Extraction + as-of RAG:", ev["decision"],
          "| model:", ev["model_version"])
    print("   OCR evidence spans:", sum(1 for s in ev["evidence_spans"] if "page" in s),
          "| verified rule citations:",
          sum(1 for c in ev["rule_citations"] if c.get("verified")),
          "/", len(ev["rule_citations"]))
    print("   corpus integrity:", verify_corpus_integrity(conn)["ok"])
    noisy = valid_stub_response()
    noisy["documents"]["financial_proposal"]["fields"]["amount"] = "950000"
    ev2, _ = extract_and_check(conn, tx, checklist, pages, StubBackend(noisy), rag=rag)
    print("   model returns wrong type ->", ev2["decision"], "(abstain, not disqualify)")
    hit = rag.retrieve("confidentiality of submitted bids", tx, top_k=1)[0]
    print("   Q&A retrieval -> Sec.", hit["section"], "|", hit["law"])

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
    print("9. Deterministic drafting:", draft["status"])
    print("   Notice of Award is a", noa_class, "form (NOT a Standard Form);",
          "official body not registered -> release blocked")

    from monitoring import (
        register_contract, register_milestones, record_milestone_completion,
        raise_variation_order, dashboard, export_dashboard,
    )
    impl = create_transaction(
        conn, regime="RA12009_IRR", posting_date="2026-11-05",
        mode="Competitive Bidding", award_criterion="LCRB",
        drafter_id="staff-01",
        legal_basis_note="Legal officer memo: RA 12009 applies",
        stage="CONTRACT")
    register_contract(conn, impl, contract_amount=1_000_000,
                      start_date="2026-01-01", completion_date="2026-03-01",
                      approved_by="hope-01", ld_rate_per_day=0.001,
                      ld_cap_ratio=0.10)
    ms = register_milestones(conn, impl, [
        {"name": "Delivery of units", "due_date": "2026-02-01", "amount": 600000},
        {"name": "Installation", "due_date": "2026-11-05", "amount": 300000},
        {"name": "Training", "due_date": "2026-11-20", "amount": 100000},
    ], approved_by="project-manager-01")
    record_milestone_completion(conn, ms[0], "2026-02-10", "doc-evidence-1",
                                "inspector-01")
    raise_variation_order(conn, impl, reference="VO-001",
                          description="Additional units", reason="Scope change",
                          requested_by="project-manager-01",
                          requested_amount=50000)
    board = dashboard(conn, "2026-11-05")
    print("10. Monitoring dashboard:", board["contracts_tracked"], "contract(s) |",
          "critical alerts:", board["alerts_by_severity"]["critical"],
          "| open VOs:", board["open_variation_orders"])
    ld = board["contracts"][0]["liquidated_damages"]
    print("    LD estimate:", ld["amount"], "|", ld["status"])
    export_dashboard(conn, "sample-dashboard.html", "2026-11-05")

    from learning import (
        submit_correction, adjudicate_correction, register_benchmark_cases,
        create_training_batch, register_candidate, evaluate_candidate,
        approve_candidate, deploy_candidate, promote_canary,
        current_production, learning_dashboard,
    )
    register_benchmark_cases(conn, [
        {"case_id": f"{sl}-{i}", "slice_name": sl,
         "input": {"document": f"historical case {i}"},
         "expected": {"decision": "PASS"},
         "source_ref": "anonymized historical BAC case"}
        for sl in ("philgeps_registration", "omnibus_sworn_statement")
        for i in range(10)
    ], "ml-lead-01")
    for i in range(4):
        c = submit_correction(
            conn, tx, verdict_id="demo-verdict", field_path=f"documents.omnibus.{i}",
            ai_value=True, correct_value=False, reason="BAC override confirmed",
            submitted_by="staff-01")
        adjudicate_correction(conn, c["correction_id"], "ACCEPTED",
                              "reviewer-01", "checked against the physical folder")
    batch = create_training_batch(conn, created_by="ml-engineer-01",
                                  base_training_set="original-train-v1")
    register_candidate(conn, version="model-v1",
                       training_batch_id=batch["batch_id"],
                       q_lora_config={"base_training_set": "original-train-v1",
                                      "replay_ratio": 0.3, "lora_r": 16},
                       corpus_snapshot_id="corpus-ra12009-2025-02")
    evl = evaluate_candidate(conn, "model-v1", {
        "philgeps_registration": {"correct": 10, "total": 10},
        "omnibus_sworn_statement": {"correct": 9, "total": 10},
    }, "ci-runner-01")
    approve_candidate(conn, "model-v1", "legal-officer-01", "approved for canary")
    deploy_candidate(conn, "model-v1", "ml-engineer-01", strategy="canary")
    promote_canary(conn, "model-v1", "bac-chair-01")
    print("11. Human-gated retraining loop: eval passed:", evl["passed"],
          "| production:", current_production(conn)["version"],
          "| corrections:", batch["correction_count"])

    from audit import seal_folder, verify_sealed_folder, compile_folder
    from coa_server import handle
    seal = seal_folder(conn, tx, "bac-secretariat-01")
    coa = verify_sealed_folder(conn, seal["id"])
    print("12. COA folder sealed (WORM): version", seal["version"],
          "| verification:", coa["ok"],
          "| completeness flags:",
          len(compile_folder(conn, tx)["meta_completeness"]["flags"]))
    print("    read-only COA endpoint:",
          handle(conn, "/verify-chain")[1]["ok"],
          "|", handle(conn, "/integrity")[1]["ok"])

    folder = export_folder(conn, tx)
    print("13. Audit folder:", folder["manifest"]["event_count"], "events |",
          "chain ok:", folder["manifest"]["chain_ok"], "|",
          "folder hash:", folder["manifest"]["folder_hash"][:16], "...")

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "vps.db")
        report = migrate(conn, target, actor_id="it-admin-01")
        print("14. Local -> VPS migration:", report["events_checked"],
              "events copied | chain ok:", report["ok"])

    print("\nDone. No external AI/embedding API was called at any point.")


if __name__ == "__main__":
    main()
