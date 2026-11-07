"""Acceptance tests A1-A15 from 03-tech-spec-rules-evidence-gate.md.

Run:  python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
from checker import evaluate_submission, sample_checklist
from forms import (
    render_draft, register_official_form, apply_human_edit, cross_check_fields,
    list_forms, NOT_REGISTERED, DRAFT_UNSIGNED,
)
from extraction import (
    StubBackend, LocalHTTPBackend, PassthroughOCR, ExtractionError,
    extract_and_check, validate_output, load_schema, valid_stub_response,
)
from rag import AsOfRAG, StubEmbedding, verify_corpus_integrity
from audit import (
    compile_folder, seal_folder, verify_sealed_folder, export_package,
    list_seals, meta_completeness,
)
from coa_server import handle, _assert_loopback, main as coa_main
from monitoring import (
    register_contract, register_milestones, record_milestone_completion,
    raise_variation_order, review_variation_order, compute_status, dashboard,
    render_dashboard_html, export_dashboard,
)
from learning import (
    submit_correction, adjudicate_correction, flag_citation,
    resolve_citation_feedback, register_benchmark_cases, benchmark_snapshot_hash,
    create_training_batch, register_candidate, evaluate_candidate,
    approve_candidate, deploy_candidate, promote_canary, rollback,
    current_production, learning_dashboard, verdict_provenance,
)
from core import (
    CoreError, GateBlocked, PolicyBlocked, RuleResolutionError, UnverifiedRuleError,
    canonical, init_db, suggest_regime, resolve_rules, get_rule,
    create_transaction, get_tx, ingest_document, record_bid_opening,
    record_financial_opening, add_signoff, request_transition, submit_verdict,
    export_folder, verify_chain, migrate, issue_number, sha256_text, append_event,
)

PROC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H1 = sha256_text("minutes-v1")
H2 = sha256_text("draft-v1")
H3 = sha256_text("draft-v2")


def new_tx(conn, **kw):
    params = dict(
        regime="RA12009_IRR", posting_date="2026-11-05",
        mode="Competitive Bidding", award_criterion="LCRB",
        drafter_id="staff-01",
        legal_basis_note="Legal officer memo: RA 12009 applies to new postings",
    )
    params.update(kw)
    return create_transaction(conn, **params)


def open_bids(conn, tx):
    request_transition(conn, tx, "OPENING_SCHEDULED", H1)
    record_bid_opening(conn, tx, "bac-chair-01", "minutes-1",
                       ["TECHNICAL_ENVELOPE"], ["bac-chair-01"])
    add_signoff(conn, tx, "OPENING_SCHEDULED", "CONTENT_INGESTION",
                "bac-chair-01", "BAC_CHAIR", H1)
    request_transition(conn, tx, "CONTENT_INGESTION", H1)


def sample_verdict(resolved, **overrides):
    v = dict(
        status="PASS", decision="PASS", confidence=0.95,
        missing_documents=[], invalid_documents=[], notes=[],
        evidence_spans=[{"field": "omnibus.notarized", "page": 4}],
        rule_citations=[{"section": "IRR RA 12009 Sec. 59.1", "verified": True}],
        model_version="qwen2.5-3b-ft-2026-10-01",
        ruleset_version=resolved["ruleset_version"],
        corpus_snapshot_id=resolved["corpus_snapshot_id"],
        prompt_hash=sha256_text("prompt-v1"),
    )
    v.update(overrides)
    return v


class AcceptanceTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.r = resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")

    def tearDown(self):
        self.conn.close()

    # A1 - legacy regime fails closed on unverified rules
    def test_A1_legacy_rules_fail_closed(self):
        legacy = resolve_rules(self.conn, "2023-06-01", "RA9184_2016IRR")
        self.assertEqual(legacy["regime"], "RA9184_2016IRR")
        with self.assertRaises(UnverifiedRuleError):
            get_rule(self.conn, legacy, "SVP_THRESHOLD")
        self.assertEqual(suggest_regime(self.conn, "2023-06-01")["regime"], "AMBIGUOUS")

    # A2 - current regime resolves verified values
    def test_A2_current_regime_values(self):
        self.assertEqual(get_rule(self.conn, self.r, "SVP_THRESHOLD")["amount"], 2000000)
        self.assertEqual(get_rule(self.conn, self.r, "SVP_MIN_QUOTATIONS_TO_PROCEED"), 1)
        self.assertEqual(get_rule(self.conn, self.r, "POSTING_DAYS_COMPETITIVE"), 7)
        self.assertIn("MEARB", get_rule(self.conn, self.r, "AWARD_CRITERIA"))

    # A3 - a new ruleset does not re-pin an existing transaction
    def test_A3_new_ruleset_does_not_repin(self):
        tx = new_tx(self.conn)
        pinned = get_tx(self.conn, tx)["ruleset_version"]
        self.conn.execute(
            "INSERT INTO ruleset (id,regime,version,effective_from,effective_to,"
            "issuance_ref,checksum,approved_by,approved_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("rs-new", "RA12009_IRR", "RA12009_IRR@2027-01", "2027-01-01", None,
             "future issuance", "x", "legal", core.now()),
        )
        self.assertEqual(get_tx(self.conn, tx)["ruleset_version"], pinned)
        self.assertEqual(resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")["ruleset_version"], pinned)
        self.assertEqual(resolve_rules(self.conn, "2027-02-01", "RA12009_IRR")["ruleset_version"],
                         "RA12009_IRR@2027-01")

    # A4 - sealed bid content blocked before opening
    def test_A4_ingest_blocked_before_opening(self):
        tx = new_tx(self.conn)
        with self.assertRaises(PolicyBlocked):
            ingest_document(self.conn, tx, "BID_CONTENT", "sealed", {"bidder": "A"})
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='POLICY_BLOCKED'",
            (tx,)).fetchone()["c"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM document WHERE tx_id=?", (tx,)).fetchone()["c"], 0)

    # A5 - ingest succeeds after public opening
    def test_A5_ingest_after_opening(self):
        tx = new_tx(self.conn)
        open_bids(self.conn, tx)
        doc = ingest_document(self.conn, tx, "BID_CONTENT", "opened", {"bidder": "A"})
        self.assertIsNotNone(doc["opening_seq"] if "opening_seq" in doc else doc["document_id"])
        row = self.conn.execute("SELECT opening_seq FROM document WHERE id=?",
                                (doc["document_id"],)).fetchone()
        self.assertIsNotNone(row["opening_seq"])

    # A6 - MEARB financial envelopes stay sealed
    def test_A6_financial_envelope_sealed(self):
        tx = new_tx(self.conn, award_criterion="MEARB")
        open_bids(self.conn, tx)
        with self.assertRaises(PolicyBlocked):
            ingest_document(self.conn, tx, "FINANCIAL_ENVELOPE", "financial", {})
        record_financial_opening(self.conn, tx, "bac-chair-01", "minutes-2")
        result = ingest_document(self.conn, tx, "FINANCIAL_ENVELOPE", "financial", {})
        self.assertTrue(result["document_id"])

    # A7 - transition without signoff blocked
    def test_A7_gate_without_signoff(self):
        tx = new_tx(self.conn)
        open_bids(self.conn, tx)
        with self.assertRaises(GateBlocked):
            request_transition(self.conn, tx, "EVALUATION", H2)

    # A8 - editing a signed draft invalidates the signoff
    def test_A8_edit_invalidates_signoff(self):
        tx = new_tx(self.conn)
        open_bids(self.conn, tx)
        add_signoff(self.conn, tx, "CONTENT_INGESTION", "EVALUATION",
                    "bac-chair-01", "BAC_CHAIR", H2)
        with self.assertRaises(GateBlocked):
            request_transition(self.conn, tx, "EVALUATION", H3)
        request_transition(self.conn, tx, "EVALUATION", H2)
        self.assertEqual(get_tx(self.conn, tx)["stage"], "EVALUATION")

    # A9 - drafter cannot self-approve
    def test_A9_segregation_of_duties(self):
        tx = new_tx(self.conn, drafter_id="staff-01")
        open_bids(self.conn, tx)
        add_signoff(self.conn, tx, "CONTENT_INGESTION", "EVALUATION",
                    "staff-01", "BAC_CHAIR", H2)
        with self.assertRaises(GateBlocked):
            request_transition(self.conn, tx, "EVALUATION", H2)
        add_signoff(self.conn, tx, "CONTENT_INGESTION", "EVALUATION",
                    "bac-chair-01", "BAC_CHAIR", H2)
        request_transition(self.conn, tx, "EVALUATION", H2)

    # A10 - low confidence / unverified citation forces ABSTAIN
    def test_A10_abstain(self):
        tx = new_tx(self.conn)
        v = submit_verdict(self.conn, tx, sample_verdict(self.r, confidence=0.40))
        self.assertEqual(v["decision"], "ABSTAIN")
        self.assertTrue(v["advisory"])
        v2 = submit_verdict(self.conn, tx, sample_verdict(
            self.r, rule_citations=[{"section": "x", "verified": False}]))
        self.assertEqual(v2["decision"], "ABSTAIN")

    # A11 - tampering with the ledger is detected
    def test_A11_tamper_detected(self):
        tx = new_tx(self.conn)
        self.assertTrue(verify_chain(self.conn)["ok"])
        self.conn.execute("DROP TRIGGER ledger_no_update")
        self.conn.execute("UPDATE ledger_event SET payload='{}' WHERE seq=1")
        report = verify_chain(self.conn)
        self.assertFalse(report["ok"])
        self.assertTrue(any(e["problem"] == "row_hash mismatch" for e in report["errors"]))

    # A12 - verdict replay is byte-identical
    def test_A12_verdict_replay(self):
        tx = new_tx(self.conn)
        submit_verdict(self.conn, tx, sample_verdict(self.r))
        submit_verdict(self.conn, tx, sample_verdict(self.r))
        rows = self.conn.execute("SELECT payload FROM verdict WHERE tx_id=?", (tx,)).fetchall()
        self.assertEqual(rows[0]["payload"], rows[1]["payload"])

    # A13 - as-of resolution rejects dates before the ruleset existed
    def test_A13_as_of_filter(self):
        with self.assertRaises(RuleResolutionError):
            resolve_rules(self.conn, "2024-01-01", "RA12009_IRR")

    # A14 - numbering is gap-free under concurrency
    def test_A14_concurrent_numbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "numbers.db")
            init_db(path).close()
            errors = []
            numbers = []
            lock = threading.Lock()

            def worker(i):
                try:
                    c = core._connect(path)
                    n = issue_number(c, "RFQ", "RO2", 2026)
                    with lock:
                        numbers.append(n)
                    c.close()
                except Exception as exc:
                    with lock:
                        errors.append(str(exc))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(numbers), 20)
            self.assertEqual(len(set(numbers)), 20)
            ordinals = sorted(int(n.split("-")[-1]) for n in numbers)
            self.assertEqual(ordinals, list(range(1, 21)))

    # A15 - migration preserves the hash chain
    def test_A15_migration_preserves_chain(self):
        tx = new_tx(self.conn)
        open_bids(self.conn, tx)
        ingest_document(self.conn, tx, "BID_CONTENT", "opened", {"bidder": "A"})
        submit_verdict(self.conn, tx, sample_verdict(self.r))
        before = verify_chain(self.conn)
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "vps.db")
            report = migrate(self.conn, target)
            self.assertTrue(report["ok"])
            self.assertEqual(report["events_checked"], before["events_checked"])
            target_conn = core._connect(target)
            self.assertTrue(verify_chain(target_conn)["ok"])
            self.assertEqual(
                target_conn.execute("SELECT ruleset_version FROM proc_tx WHERE id=?", (tx,))
                .fetchone()["ruleset_version"], self.r["ruleset_version"])
            target_conn.close()

    # regression - chain head must be read inside the write transaction
    def test_concurrent_event_appends_keep_one_valid_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "concurrent.db")
            init_db(db).close()
            errors = []

            def worker(i):
                try:
                    c = core._connect(db)
                    append_event(c, "SYSTEM", "CONCURRENT_TEST", f"worker-{i}",
                                 "TEST", {"i": i})
                    c.close()
                except Exception as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            conn = core._connect(db)
            try:
                report = verify_chain(conn)
                self.assertTrue(report["ok"], report["errors"])
                self.assertEqual(report["events_checked"], 20)
                heads = conn.execute(
                    "SELECT prev_hash, count(*) c FROM ledger_event GROUP BY prev_hash HAVING c > 1"
                ).fetchall()
                self.assertEqual(heads, [], "two events chained off the same head")
            finally:
                conn.close()

    # extra - e-bid passwords are rejected outright
    def test_password_never_stored(self):
        tx = new_tx(self.conn)
        open_bids(self.conn, tx)
        with self.assertRaises(PolicyBlocked):
            ingest_document(self.conn, tx, "BID_CONTENT", "x", {"password": "secret"})

    # extra - full audit folder is coherent
    def test_audit_folder(self):
        tx = new_tx(self.conn)
        open_bids(self.conn, tx)
        folder = export_folder(self.conn, tx)
        self.assertTrue(folder["manifest"]["chain_ok"])
        self.assertGreaterEqual(folder["manifest"]["event_count"], 5)
        self.assertEqual(folder["transaction"]["regime"], "RA12009_IRR")
        self.assertEqual(folder["manifest"]["folder_hash"],
                         sha256_text(canonical({k: folder[k] for k in
                                                ("transaction", "events", "documents",
                                                 "signoffs", "verdicts", "drafts")})))


def complete_extraction():
    return {"as_of": "2026-11-05", "documents": {
        "philgeps_registration": {"present": True, "valid_until": "2027-06-30"},
        "omnibus_sworn_statement": {"present": True, "signed": True, "notarized": True},
        "bid_securing_declaration": {"present": True, "signed": True},
        "financial_proposal": {"present": True, "amount": 950000},
    }}


class CheckerTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.r = resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")
        self.tx = new_tx(self.conn)
        open_bids(self.conn, self.tx)
        self.checklist = sample_checklist(self.r["ruleset_version"])

    def tearDown(self):
        self.conn.close()

    def test_complete_is_pass_and_advisory(self):
        v = evaluate_submission(self.conn, self.tx, self.checklist, complete_extraction())
        self.assertEqual(v["decision"], "PASS")
        self.assertTrue(v["advisory"])
        self.assertEqual(v["model_version"], "rules-engine-1.0-no-llm")
        self.assertTrue(v["evidence_spans"])

    def test_expired_document_is_fail(self):
        ext = complete_extraction()
        ext["documents"]["philgeps_registration"]["valid_until"] = "2025-01-01"
        v = evaluate_submission(self.conn, self.tx, self.checklist, ext)
        self.assertEqual(v["decision"], "FAIL")
        self.assertIn("philgeps_registration", v["invalid_documents"])

    def test_missing_document_is_fail(self):
        ext = complete_extraction()
        del ext["documents"]["bid_securing_declaration"]
        v = evaluate_submission(self.conn, self.tx, self.checklist, ext)
        self.assertEqual(v["decision"], "FAIL")
        self.assertIn("bid_securing_declaration", v["missing_documents"])

    def test_amount_over_abc_is_fail(self):
        ext = complete_extraction()
        ext["documents"]["financial_proposal"]["amount"] = 1200000
        v = evaluate_submission(self.conn, self.tx, self.checklist, ext)
        self.assertEqual(v["decision"], "FAIL")
        self.assertIn("financial_proposal", v["invalid_documents"])

    def test_unreadable_scan_abstains_not_disqualifies(self):
        ext = complete_extraction()
        ext["documents"]["omnibus_sworn_statement"] = {"status": "UNREADABLE"}
        v = evaluate_submission(self.conn, self.tx, self.checklist, ext)
        self.assertEqual(v["decision"], "ABSTAIN")
        self.assertNotIn("omnibus_sworn_statement", v["missing_documents"])
        self.assertNotIn("omnibus_sworn_statement", v["invalid_documents"])

    def test_unextracted_field_abstains(self):
        ext = complete_extraction()
        del ext["documents"]["omnibus_sworn_statement"]["signed"]
        v = evaluate_submission(self.conn, self.tx, self.checklist, ext)
        self.assertEqual(v["decision"], "ABSTAIN")

    def test_unapproved_checklist_rejected(self):
        bad = dict(self.checklist)
        bad.pop("approved_by")
        with self.assertRaises(CoreError):
            evaluate_submission(self.conn, self.tx, bad, complete_extraction())

    def test_legacy_regime_fails_closed(self):
        legacy = new_tx(self.conn, regime="RA9184_2016IRR", posting_date="2023-06-01")
        with self.assertRaises(UnverifiedRuleError):
            evaluate_submission(self.conn, legacy,
                                sample_checklist("RA9184_2016IRR@2016-10-28"),
                                complete_extraction())


def noa_fields(amount=950000):
    return {
        "approved_by": "bac-secretariat-01",
        "values": {"awardee_name": "ACME Corp", "project_title": "Laptop supply",
                   "contract_amount": amount, "hop_e_signature": "PENDING"},
        "source_refs": {"awardee_name": "BAC Resolution 2026-011",
                        "project_title": "Approved PR RO2-PR-2026-00001",
                        "contract_amount": "Abstract of Quotations 2026-011",
                        "hop_e_signature": "HOPE signature sheet"},
    }


class FormTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.tx = new_tx(self.conn)
        open_bids(self.conn, self.tx)

    def tearDown(self):
        self.conn.close()

    def test_registry_classification_matches_gppb_resolution(self):
        standard = {f["code"] for f in list_forms(self.conn, classification="STANDARD")}
        recommended = {f["code"] for f in list_forms(self.conn, classification="RECOMMENDED")}
        self.assertIn("APP", standard)
        self.assertIn("OSS", standard)
        self.assertIn("PBD_GOODS", standard)
        for code in ("NOTICE_OF_AWARD", "ABSTRACT_OF_BIDS", "RFQ_REI"):
            self.assertIn(code, recommended, f"{code} is a RECOMMENDED form, not STANDARD")
        self.assertEqual(len(standard) + len(recommended), 36)

    def test_unregistered_official_body_blocks_release(self):
        with self.assertRaises(CoreError):
            render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", noa_fields(),
                         for_release=True)

    def test_structural_draft_is_labelled_and_advisory(self):
        draft = render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", noa_fields())
        self.assertEqual(draft["status"], NOT_REGISTERED)
        self.assertTrue(draft["AI_DRAFTED"])
        self.assertIn("NOT A LEGAL FORM", draft["body"])
        self.assertIn("source: BAC Resolution 2026-011", draft["body"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='AI_DRAFT_CREATED'",
            (self.tx,)).fetchone()["c"], 1)

    def test_unknown_and_missing_fields_are_rejected_not_invented(self):
        bad = noa_fields()
        bad["values"]["invented_clause"] = "something the model made up"
        with self.assertRaises(CoreError):
            render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", bad)
        bad2 = noa_fields()
        del bad2["values"]["contract_amount"]
        with self.assertRaises(CoreError):
            render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", bad2)
        bad3 = noa_fields()
        del bad3["source_refs"]["awardee_name"]
        with self.assertRaises(CoreError):
            render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", bad3)

    def test_form_not_applicable_to_mode(self):
        with self.assertRaises(CoreError):
            render_draft(self.conn, self.tx, "RFQ_REI", {
                "approved_by": "bac-secretariat-01",
                "values": {"pr_number": "PR-1", "item_description": "Laptops",
                           "abc": 900000, "submission_deadline": "2026-11-20"},
                "source_refs": {"pr_number": "PR", "item_description": "PR",
                                "abc": "APP", "submission_deadline": "RFQ schedule"},
            })

    def test_human_edit_changes_hash_and_invalidates_signoff(self):
        draft = render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", noa_fields())
        old = draft["content_hash"]
        add_signoff(self.conn, self.tx, "CONTENT_INGESTION", "EVALUATION",
                    "bac-chair-01", "BAC_CHAIR", old)
        edit = apply_human_edit(self.conn, draft["draft_id"],
                                draft["body"] + "\nHuman correction.\n", "bac-secretariat-01")
        self.assertNotEqual(old, edit["new_hash"])
        with self.assertRaises(GateBlocked):
            request_transition(self.conn, self.tx, "EVALUATION", old)
        with self.assertRaises(GateBlocked):
            request_transition(self.conn, self.tx, "EVALUATION", edit["new_hash"])
        add_signoff(self.conn, self.tx, "CONTENT_INGESTION", "EVALUATION",
                    "bac-chair-01", "BAC_CHAIR", edit["new_hash"])
        request_transition(self.conn, self.tx, "EVALUATION", edit["new_hash"])
        self.assertEqual(get_tx(self.conn, self.tx)["stage"], "EVALUATION")

    def test_registered_official_form_allows_release_draft(self):
        register_official_form(self.conn, "NOTICE_OF_AWARD", "a" * 64,
                               "GPPB-03-2025-NOA-v1", "bac-secretariat-01")
        draft = render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", noa_fields(),
                             for_release=True)
        self.assertEqual(draft["status"], DRAFT_UNSIGNED)
        self.assertIn("UNSIGNED", draft["body"])

    def test_cross_check_detects_contract_vs_bidding_doc_mismatch(self):
        render_draft(self.conn, self.tx, "NOTICE_OF_AWARD", noa_fields(amount=950000))
        render_draft(self.conn, self.tx, "CONTRACT_FORM", {
            "approved_by": "bac-secretariat-01",
            "values": {"contract_number": "C-2026-001", "supplier_name": "ACME Corp",
                       "contract_amount": 990000, "hop_e_signature": "PENDING"},
            "source_refs": {"contract_number": "Numbering service",
                            "supplier_name": "NOA", "contract_amount": "Contract draft",
                            "hop_e_signature": "HOPE signature sheet"},
        })
        result = cross_check_fields(self.conn, self.tx, "NOTICE_OF_AWARD",
                                    "CONTRACT_FORM", ["contract_amount"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatches"][0]["field"], "contract_amount")


PAGES = [{"page": 1, "text": "PhilGEPS Platinum, valid until 2027-06-30"},
         {"page": 2, "text": "Financial proposal: PHP 950,000.00"}]


class ExtractionTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.r = resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")
        self.tx = new_tx(self.conn)
        self.checklist = sample_checklist(self.r["ruleset_version"])

    def tearDown(self):
        self.conn.close()

    def _run(self, response, **kw):
        open_bids(self.conn, self.tx)
        return extract_and_check(self.conn, self.tx, self.checklist, PAGES,
                                 StubBackend(response), **kw)

    def test_valid_extraction_passes_with_ocr_evidence(self):
        verdict, result = self._run(valid_stub_response())
        self.assertEqual(verdict["decision"], "PASS")
        self.assertEqual(verdict["model_version"], "qwen2.5-3b-ft-procurement-v1")
        self.assertEqual(verdict["rules_engine_version"], "rules-engine-1.0-no-llm")
        self.assertFalse(result.violations)
        self.assertTrue(any("page" in s for s in verdict["evidence_spans"]))

    def test_low_confidence_field_abstains_not_disqualifies(self):
        response = valid_stub_response()
        response["documents"]["omnibus_sworn_statement"]["confidence"]["signed"] = 0.35
        verdict, result = self._run(response)
        self.assertEqual(verdict["decision"], "ABSTAIN")
        self.assertIn("omnibus_sworn_statement.signed", result.uncertain_fields[0])
        self.assertNotIn("omnibus_sworn_statement", verdict["missing_documents"])

    def test_unreadable_document_abstains(self):
        response = valid_stub_response()
        response["documents"]["philgeps_registration"] = {"status": "UNREADABLE"}
        verdict, _ = self._run(response)
        self.assertEqual(verdict["decision"], "ABSTAIN")
        self.assertNotIn("philgeps_registration", verdict["invalid_documents"])

    def test_hallucinated_field_causes_abstain(self):
        response = valid_stub_response()
        response["documents"]["financial_proposal"]["fields"]["invented_clause"] = "free text"
        verdict, result = self._run(response)
        self.assertEqual(verdict["decision"], "ABSTAIN")
        self.assertTrue(any("hallucinated fields" in v for v in result.violations))

    def test_wrong_type_causes_abstain(self):
        response = valid_stub_response()
        response["documents"]["financial_proposal"]["fields"]["amount"] = "950000"
        verdict, _ = self._run(response)
        self.assertEqual(verdict["decision"], "ABSTAIN")

    def test_missing_model_version_causes_abstain(self):
        response = valid_stub_response()
        del response["model_version"]
        verdict, _ = self._run(response)
        self.assertEqual(verdict["decision"], "ABSTAIN")

    def test_adapter_owns_prompt_hash(self):
        response = valid_stub_response()
        response["prompt_hash"] = "model-supplied-value"
        verdict, _ = self._run(response)
        self.assertNotEqual(verdict["prompt_hash"], "model-supplied-value")
        self.assertEqual(len(verdict["prompt_hash"]), 64)

    def test_external_ai_endpoint_refused(self):
        with self.assertRaises(CoreError):
            LocalHTTPBackend("https://api.openai.example/extract")
        with self.assertRaises(CoreError):
            LocalHTTPBackend("http://192.168.1.50:8080/extract")
        self.assertEqual(LocalHTTPBackend("http://127.0.0.1:8080/extract").url,
                         "http://127.0.0.1:8080/extract")

    def test_extraction_before_opening_blocked(self):
        with self.assertRaises(PolicyBlocked):
            extract_and_check(self.conn, self.tx, self.checklist, PAGES,
                              StubBackend(valid_stub_response()))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM verdict WHERE tx_id=?", (self.tx,)).fetchone()["c"], 0)

    def test_backend_failure_produces_no_verdict(self):
        open_bids(self.conn, self.tx)
        def boom(pages, schema):
            raise ExtractionError("local model server offline")
        with self.assertRaises(ExtractionError):
            extract_and_check(self.conn, self.tx, self.checklist, PAGES, StubBackend(boom))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM verdict WHERE tx_id=?", (self.tx,)).fetchone()["c"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='EXTRACTION_FAILED'",
            (self.tx,)).fetchone()["c"], 1)

    def test_ledger_records_extraction_events(self):
        self._run(valid_stub_response())
        types = {r["event_type"] for r in self.conn.execute(
            "SELECT event_type FROM ledger_event WHERE tx_id=?", (self.tx,)).fetchall()}
        self.assertIn("OCR_COMPLETED", types)
        self.assertIn("EXTRACTION_COMPLETED", types)

    def test_schema_validation_failure_is_logged(self):
        response = valid_stub_response()
        response["documents"]["ghost_document"] = {"status": "OK", "fields": {}}
        self._run(response)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='SCHEMA_VALIDATION_FAILED'",
            (self.tx,)).fetchone()["c"], 1)


class RAGTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.rag = AsOfRAG(self.conn)
        self.r = resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")
        self.tx = new_tx(self.conn)
        self.checklist = sample_checklist(self.r["ruleset_version"])

    def tearDown(self):
        self.conn.close()

    def _insert_chunk(self, chunk_id, **overrides):
        base = dict(
            chunk_id=chunk_id, corpus_snapshot_id="corpus-ra12009-2025-02",
            law="IRR of RA 12009", issuance_no="IRR of RA 12009",
            section="34.1", regime="RA12009_IRR", effective_from="2025-02-01",
            effective_to=None, supersedes=None,
            source_url="https://www.gppb.gov.ph",
            text="Small Value Procurement threshold two million pesos and three price quotations.",
            passage_hash="", verified=1,
        )
        base.update(overrides)
        base["passage_hash"] = sha256_text(base["text"])
        cols = ",".join(base)
        self.conn.execute(
            f"INSERT OR REPLACE INTO corpus_chunk ({cols}) VALUES ({','.join('?' for _ in base)})",
            tuple(base.values()),
        )

    def test_retrieval_finds_the_right_section(self):
        hits = self.rag.retrieve("Sec. 59.1 checklist of required documents pass or fail",
                                 self.tx, top_k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["section"], "59.1")

    def test_as_of_filters_future_and_expired_rules(self):
        self._insert_chunk("FUTURE-RULE", effective_from="2030-01-01")
        self._insert_chunk("EXPIRED-RULE", effective_to="2020-01-01")
        ids = {r["chunk_id"] for r in self.rag._candidates(self.tx)}
        self.assertNotIn("FUTURE-RULE", ids)
        self.assertNotIn("EXPIRED-RULE", ids)
        self.assertIn("RA12009-IRR-S59-1", ids)

    def test_superseded_chunk_is_excluded(self):
        self._insert_chunk("NEW-THRESHOLD", section="34.1",
                           supersedes="RA12009-IRR-S34-1",
                           effective_from="2026-01-01")
        ids = {r["chunk_id"] for r in self.rag._candidates(self.tx)}
        self.assertNotIn("RA12009-IRR-S34-1", ids)
        self.assertIn("NEW-THRESHOLD", ids)

    def test_citation_resolves_to_a_verified_passage(self):
        citation = self.rag.resolve_citation(
            "IRR RA 12009 Sec. 59.1 / ITB checklist", self.tx)
        self.assertTrue(citation["verified"])
        self.assertEqual(citation["chunk_id"], "RA12009-IRR-S59-1")
        self.assertEqual(self.rag.verify(citation, self.tx), (True, "verified"))

    def test_unknown_section_cannot_produce_a_verified_citation(self):
        citation = self.rag.resolve_citation("IRR RA 12009 Sec. 999.9", self.tx)
        self.assertFalse(citation["verified"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='CITATION_MISMATCH'",
            (self.tx,)).fetchone()["c"], 1)

    def test_legacy_regime_has_no_citable_verified_corpus(self):
        legacy_tx = new_tx(self.conn, regime="RA9184_2016IRR",
                           posting_date="2023-06-01")
        citation = self.rag.resolve_citation("2016 IRR Annex H Sec. 53.9", legacy_tx)
        self.assertFalse(citation["verified"])

    def test_tampered_passage_is_detected(self):
        citation = self.rag.resolve_citation("IRR RA 12009 Sec. 58.5", self.tx)
        self.conn.execute("UPDATE corpus_chunk SET text=? WHERE chunk_id=?",
                          ("tampered", citation["chunk_id"]))
        ok, reason = self.rag.verify(citation, self.tx)
        self.assertFalse(ok)
        self.assertIn("altered", reason)

    def test_corpus_integrity_check(self):
        self.assertTrue(verify_corpus_integrity(self.conn)["ok"])
        self.conn.execute("UPDATE corpus_chunk SET text='tampered' WHERE chunk_id='RA12009-IRR-S34-1'")
        report = verify_corpus_integrity(self.conn)
        self.assertFalse(report["ok"])
        self.assertIn("RA12009-IRR-S34-1", report["altered_chunks"])

    def test_full_pipeline_with_rag_cites_every_rule(self):
        open_bids(self.conn, self.tx)
        verdict, _ = extract_and_check(
            self.conn, self.tx, self.checklist, PAGES,
            StubBackend(valid_stub_response()), rag=self.rag)
        self.assertEqual(verdict["decision"], "PASS")
        self.assertTrue(all(c.get("verified") for c in verdict["rule_citations"]))
        self.assertTrue(all(c.get("chunk_id") for c in verdict["rule_citations"]))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='RAG_CITATIONS_RESOLVED'",
            (self.tx,)).fetchone()["c"], 1)

    def test_unverifiable_citation_forces_abstain(self):
        open_bids(self.conn, self.tx)
        bad_checklist = dict(self.checklist)
        bad_checklist["documents"] = [
            {**d, "section_ref": "IRR RA 12009 Sec. 999.9"}
            for d in self.checklist["documents"]
        ]
        verdict, _ = extract_and_check(
            self.conn, self.tx, bad_checklist, PAGES,
            StubBackend(valid_stub_response()), rag=self.rag)
        self.assertEqual(verdict["decision"], "ABSTAIN")

    def test_snapshot_activation_requires_a_human(self):
        with self.assertRaises(CoreError):
            self.rag.activate_snapshot("corpus-ra12009-2025-02", "")
        self.rag.activate_snapshot("corpus-ra12009-2025-02", "legal-officer-01")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE event_type='CORPUS_SNAPSHOT_ACTIVATED'"
        ).fetchone()["c"], 1)

    def test_new_corpus_does_not_repin_existing_transaction(self):
        pinned_before = get_tx(self.conn, self.tx)["corpus_snapshot_id"]
        self.rag.build_snapshot("corpus-2027", "RA12009_IRR@2027-01", "RA12009_IRR", [{
            "chunk_id": "NEW-2027", "law": "IRR of RA 12009", "issuance_no": "GPPB 2027",
            "section": "34.1", "effective_from": "2027-01-01",
            "source_url": "https://www.gppb.gov.ph",
            "text": "Revised SVP threshold issued by the GPPB.", "verified": True,
        }], approver="legal-officer-01")
        self.rag.activate_snapshot("corpus-2027", "legal-officer-01")
        self.assertEqual(get_tx(self.conn, self.tx)["corpus_snapshot_id"], pinned_before)
        new_tx_2027 = new_tx(self.conn, posting_date="2027-06-01")
        self.assertEqual(get_tx(self.conn, new_tx_2027)["corpus_snapshot_id"], "corpus-2027")

    def test_dense_backend_wiring(self):
        rag = AsOfRAG(self.conn, StubEmbedding())
        hits = rag.retrieve("confidentiality of submitted bids", self.tx, top_k=2)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["section"], "58.5")

    def test_integer_section_citation_resolves(self):
        citation = self.rag.resolve_citation("IRR RA 12009 Sec. 115", self.tx)
        self.assertTrue(citation["verified"])
        self.assertEqual(citation["chunk_id"], "RA12009-IRR-S115")

    def test_stub_embedding_is_stable_across_processes(self):
        code = (
            "import sys; sys.path.insert(0, r'" + PROC_DIR + "');"
            "from rag import StubEmbedding;"
            "print(','.join(format(v, '.8f') for v in "
            "StubEmbedding().embed('confidentiality of submitted bids')))"
        )
        def run(seed):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = str(seed)
            return subprocess.check_output(
                [sys.executable, "-c", code], env=env, text=True).strip()
        self.assertEqual(run(1), run(997))


class AuditTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.rag = AsOfRAG(self.conn)
        self.r = resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")
        self.tx = new_tx(self.conn)
        self.checklist = sample_checklist(self.r["ruleset_version"])
        open_bids(self.conn, self.tx)
        extract_and_check(self.conn, self.tx, self.checklist, PAGES,
                          StubBackend(valid_stub_response()), rag=self.rag)

    def tearDown(self):
        self.conn.close()

    def _seal(self, **kw):
        return seal_folder(self.conn, self.tx, "bac-secretariat-01", **kw)

    def test_compiled_folder_contains_full_lifecycle(self):
        folder = compile_folder(self.conn, self.tx)
        for key in ("transaction", "regime_context", "events", "documents",
                    "signoffs", "verdicts", "drafts", "rule_citations",
                    "corpus_chunks_used", "model_versions", "ledger_chain",
                    "meta_completeness", "manifest"):
            self.assertIn(key, folder)
        self.assertTrue(folder["manifest"]["folder_hash"])
        self.assertTrue(folder["manifest"]["corpus_integrity"])
        self.assertEqual(folder["regime_context"]["regime"], "RA12009_IRR")
        self.assertTrue(folder["rule_citations"])

    def test_meta_completeness_flags_missing_lifecycle_items(self):
        add_signoff(self.conn, self.tx, "CONTENT_INGESTION", "EVALUATION",
                    "bac-chair-01", "BAC_CHAIR", H2)
        request_transition(self.conn, self.tx, "EVALUATION", H2)
        folder = compile_folder(self.conn, self.tx)
        report = meta_completeness(folder)
        self.assertTrue(report["checked"])
        self.assertIn("note", report)
        items = {c["item"] for c in report["checked"]}
        self.assertIn("public_bid_opening_record", items)
        self.assertIn("evaluation_verdict", items)
        self.assertTrue(all(c["present"] for c in report["checked"]
                            if c["item"] in ("public_bid_opening_record",
                                             "evaluation_verdict")))

    def test_meta_completeness_is_stage_aware(self):
        early = meta_completeness(compile_folder(self.conn, self.tx))
        self.assertNotIn("evaluation_verdict",
                         {c["item"] for c in early["checked"]})

    def test_seal_creates_event_and_immutable_row(self):
        seal = self._seal()
        self.assertEqual(seal["version"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='FOLDER_SEALED'",
            (self.tx,)).fetchone()["c"], 1)
        self.assertEqual(len(list_seals(self.conn, self.tx)), 1)

    def test_sealing_twice_with_no_changes_is_idempotent(self):
        first = self._seal()
        second = self._seal()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(list_seals(self.conn, self.tx)), 1)

    def test_worm_requires_reason_for_a_new_version(self):
        self._seal()
        append_event(self.conn, self.tx, "LATE_CORRECTION", "staff-02",
                     "BAC_SECRETARIAT", {"note": "a late document arrived"})
        with self.assertRaises(CoreError):
            self._seal()
        second = self._seal(supersede_reason="late completion document received")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["supersedes_seal_id"], first_hash(self.conn, self.tx))
        self.assertEqual(len(list_seals(self.conn, self.tx)), 2)

    def test_verify_sealed_folder(self):
        seal = self._seal()
        report = verify_sealed_folder(self.conn, seal["id"])
        self.assertTrue(report["ok"])
        self.assertTrue(report["folder_hash_ok"])
        self.assertTrue(report["chain_ok"])
        self.assertTrue(report["chain_head_matches"])
        self.assertTrue(report["corpus_ok"])

    def test_tampered_sealed_content_is_detected(self):
        seal = self._seal()
        self.conn.execute("DROP TRIGGER sealed_folder_no_update")
        self.conn.execute("UPDATE sealed_folder SET content_json='{}' WHERE id=?",
                          (seal["id"],))
        report = verify_sealed_folder(self.conn, seal["id"])
        self.assertFalse(report["ok"])
        self.assertFalse(report["folder_hash_ok"])

    def test_export_package_and_manifest(self):
        seal = self._seal()
        with tempfile.TemporaryDirectory() as tmp:
            result = export_package(self.conn, seal["id"], tmp)
            self.assertTrue(os.path.exists(result["package"]))
            self.assertTrue(os.path.exists(result["manifest"]))
            with open(result["manifest"], encoding="utf-8") as fh:
                self.assertIn(seal["folder_hash"], fh.read())
            with open(result["package"], encoding="utf-8") as fh:
                self.assertIn("transaction", json.load(fh))

    def test_export_refuses_unverifiable_folder(self):
        seal = self._seal()
        self.conn.execute("DROP TRIGGER sealed_folder_no_update")
        self.conn.execute("UPDATE sealed_folder SET content_json='{}' WHERE id=?",
                          (seal["id"],))
        with self.assertRaises(CoreError):
            export_package(self.conn, seal["id"], tempfile.gettempdir())

    def test_coa_http_routes_are_read_only(self):
        seal = self._seal()
        before = self.conn.execute("SELECT count(*) c FROM ledger_event").fetchone()["c"]
        routes = ["/health", "/verify-chain", f"/verify-folder?seal_id={seal['id']}",
                  f"/folder?seal_id={seal['id']}", "/integrity", "/list-seals",
                  "/does-not-exist"]
        statuses = [handle(self.conn, route)[0] for route in routes]
        self.assertEqual(statuses, [200, 200, 200, 200, 200, 200, 404])
        self.assertEqual(handle(self.conn, "/verify-folder")[0], 400)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event").fetchone()["c"], before)

    def test_coa_endpoint_refuses_non_loopback_bind(self):
        with self.assertRaises(CoreError):
            _assert_loopback("0.0.0.0")
        with self.assertRaises(CoreError):
            _assert_loopback("192.168.1.10")
        self.assertIsNone(_assert_loopback("127.0.0.1"))

    def test_migration_preserves_sealed_folders(self):
        seal = self._seal()
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "vps.db")
            migrate(self.conn, target)
            tconn = core._connect(target)
            try:
                report = verify_sealed_folder(tconn, seal["id"])
                self.assertTrue(report["ok"])
                self.assertEqual(len(list_seals(tconn, self.tx)), 1)
            finally:
                tconn.close()

    def test_cli_commands_work_on_a_file_database(self):
        seal = self._seal()
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "procurement.db")
            src = init_db(db)
            tx2 = new_tx(src)
            open_bids(src, tx2)
            seal2 = seal_folder(src, tx2, "bac-secretariat-01")
            src.close()
            def cli(args):
                with contextlib.redirect_stdout(io.StringIO()):
                    return coa_main(args)
            self.assertEqual(cli(["verify-chain", "--db", db]), 0)
            self.assertEqual(cli(["verify-folder", "--db", db,
                                  "--seal-id", seal2["id"]]), 0)
            self.assertEqual(cli(["verify-corpus", "--db", db]), 0)
            self.assertEqual(cli(["export", "--db", db,
                                  "--seal-id", seal2["id"],
                                  "--out", os.path.join(tmp, "pkg")]), 0)


def first_hash(conn, tx_id):
    row = conn.execute(
        "SELECT id FROM sealed_folder WHERE tx_id=? ORDER BY version LIMIT 1",
        (tx_id,),
    ).fetchone()
    return row["id"]


AS_OF = "2026-11-05"


def contract_tx(conn, **kw):
    params = dict(regime="RA12009_IRR", posting_date="2026-11-05",
                  mode="Competitive Bidding", award_criterion="LCRB",
                  drafter_id="staff-01",
                  legal_basis_note="Legal officer memo: RA 12009 applies",
                  stage="CONTRACT")
    params.update(kw)
    return create_transaction(conn, **params)


def seeded_contract(conn, **term_kw):
    tx = contract_tx(conn)
    register_contract(conn, tx, contract_amount=1_000_000,
                      start_date="2026-01-01", completion_date="2026-03-01",
                      approved_by="hope-01", **term_kw)
    ids = register_milestones(conn, tx, [
        {"name": "Delivery of units", "due_date": "2026-02-01", "amount": 600000},
        {"name": "Installation", "due_date": AS_OF, "amount": 300000},
        {"name": "Training", "due_date": "2026-11-20", "amount": 100000},
    ], approved_by="project-manager-01")
    return tx, ids


class MonitoringTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_contract_registration_requires_stage_and_human(self):
        early = new_tx(self.conn)
        with self.assertRaises(CoreError):
            register_contract(self.conn, early, contract_amount=1000,
                              start_date="2026-01-01", completion_date="2026-02-01",
                              approved_by="hope-01")
        tx = contract_tx(self.conn)
        with self.assertRaises(CoreError):
            register_contract(self.conn, tx, contract_amount=1000,
                              start_date="2026-01-01", completion_date="2026-02-01",
                              approved_by="")
        self.assertTrue(register_contract(
            self.conn, tx, contract_amount=1000, start_date="2026-01-01",
            completion_date="2026-02-01", approved_by="hope-01"))

    def test_ld_not_configured_fails_closed(self):
        tx, _ = seeded_contract(self.conn)
        status = compute_status(self.conn, tx, AS_OF)
        ld = status["liquidated_damages"]
        self.assertEqual(ld["status"], "NOT_CONFIGURED")
        self.assertIsNone(ld["amount"])
        self.assertIn("LD_RATE_NOT_CONFIGURED", [a["code"] for a in status["alerts"]])

    def test_ld_estimate_is_advisory_and_capped(self):
        tx = contract_tx(self.conn)
        register_contract(self.conn, tx, contract_amount=1_000_000,
                          start_date="2026-01-01", completion_date="2026-03-01",
                          approved_by="hope-01", ld_rate_per_day=0.001,
                          ld_cap_ratio=0.10)
        register_milestones(self.conn, tx, [
            {"name": "Delivery", "due_date": "2026-02-01", "amount": 1_000_000},
        ], approved_by="project-manager-01")
        ld = compute_status(self.conn, tx, AS_OF)["liquidated_damages"]
        self.assertEqual(ld["status"], "ADVISORY_ESTIMATE_ONLY")
        self.assertEqual(ld["amount"], 100000.0)
        self.assertIn("NOT a determination", ld["note"])

    def test_late_completion_flags_delay_for_humans(self):
        tx, ids = seeded_contract(self.conn)
        result = record_milestone_completion(
            self.conn, ids[0], "2026-02-10", "doc-evidence-1", "inspector-01")
        self.assertEqual(result["status"], "DELAYED")
        self.assertEqual(result["days_late"], 9)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE tx_id=? AND event_type='DELAY_FLAGGED'",
            (tx,)).fetchone()["c"], 1)

    def test_milestone_completion_requires_human_verification(self):
        _, ids = seeded_contract(self.conn)
        with self.assertRaises(CoreError):
            record_milestone_completion(self.conn, ids[0], "2026-02-01",
                                        "doc-1", "")

    def test_percent_complete_and_overdue_alerts(self):
        tx, ids = seeded_contract(self.conn)
        record_milestone_completion(self.conn, ids[1], AS_OF, "doc-2", "inspector-01")
        status = compute_status(self.conn, tx, AS_OF)
        self.assertEqual(status["milestones_done"], 1)
        self.assertEqual(status["percent_complete_count"], 33.3)
        self.assertEqual(status["percent_complete_amount"], 30.0)
        self.assertIn("Delivery of units", status["overdue_milestones"])
        codes = [a["code"] for a in status["alerts"]]
        self.assertIn("MILESTONE_OVERDUE", codes)
        self.assertIn("CONTRACT_OVERRUN", codes)
        self.assertEqual(status["decision_authority"],
                         "project manager / TWG / HOPE — AI flags only")

    def test_variation_order_cannot_be_self_approved(self):
        tx, _ = seeded_contract(self.conn)
        vo = raise_variation_order(self.conn, tx, reference="VO-001",
                                   description="Additional units",
                                   reason="Scope change approved by end-user",
                                   requested_by="project-manager-01",
                                   requested_amount=50000)
        self.assertEqual(vo["status"], "PENDING")
        with self.assertRaises(CoreError):
            review_variation_order(self.conn, vo["vo_id"], "APPROVED",
                                   "project-manager-01")

    def test_variation_order_reviewed_by_a_different_human(self):
        tx, _ = seeded_contract(self.conn)
        vo = raise_variation_order(self.conn, tx, reference="VO-001",
                                   description="Additional units",
                                   reason="Scope change",
                                   requested_by="project-manager-01")
        result = review_variation_order(self.conn, vo["vo_id"], "APPROVED", "hope-01")
        self.assertEqual(result["status"], "APPROVED")
        status = compute_status(self.conn, tx, AS_OF)
        self.assertEqual(status["variation_orders"][0]["status"], "APPROVED")
        self.assertNotIn("VARIATION_ORDER_PENDING_REVIEW",
                         [a["code"] for a in status["alerts"]])

    def test_dashboard_aggregates_across_contracts(self):
        seeded_contract(self.conn)
        seeded_contract(self.conn)
        data = dashboard(self.conn, AS_OF)
        self.assertEqual(data["contracts_tracked"], 2)
        self.assertEqual(data["total_contract_value"], 2_000_000)
        self.assertEqual(data["at_risk_contracts"], 2)
        self.assertTrue(data["alerts"])

    def test_html_dashboard_is_self_contained(self):
        seeded_contract(self.conn)
        markup = render_dashboard_html(self.conn, AS_OF)
        self.assertIn("Contract implementation monitoring", markup)
        self.assertIn("PHP 1,000,000.00", markup)
        self.assertNotIn("http://", markup)
        self.assertNotIn("https://", markup)
        self.assertIn("Advisory", markup)

    def test_html_export_writes_a_file(self):
        seeded_contract(self.conn)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dashboard.html")
            export_dashboard(self.conn, path, AS_OF)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                self.assertIn("<html", fh.read())

    def test_compile_folder_includes_monitoring_records(self):
        tx, _ = seeded_contract(self.conn)
        folder = compile_folder(self.conn, tx)
        mon = folder["contract_monitoring"]
        self.assertIsNotNone(mon["terms"])
        self.assertEqual(len(mon["milestones"]), 3)
        self.assertEqual(folder["manifest"]["monitoring_milestone_count"], 3)

    def test_monitoring_tables_survive_migration(self):
        tx, ids = seeded_contract(self.conn)
        record_milestone_completion(self.conn, ids[0], "2026-02-01",
                                    "doc-1", "inspector-01")
        seal_folder(self.conn, tx, "bac-secretariat-01")
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "vps.db")
            migrate(self.conn, target)
            tconn = core._connect(target)
            try:
                self.assertEqual(tconn.execute(
                    "SELECT count(*) c FROM milestone WHERE tx_id=?", (tx,)
                ).fetchone()["c"], 3)
                self.assertEqual(tconn.execute(
                    "SELECT status FROM milestone WHERE id=?", (ids[0],)
                ).fetchone()["status"], "COMPLETED")
            finally:
                tconn.close()


BENCHMARK_CASES = [
    {"case_id": f"{sl}-{i}", "slice_name": sl,
     "input": {"document": f"historical case {sl} {i}"},
     "expected": {"decision": "PASS"},
     "source_ref": "anonymized historical BAC case"}
    for sl in ("philgeps_registration", "omnibus_sworn_statement")
    for i in range(10)
]

BASELINE = {"philgeps_registration": {"correct": 10, "total": 10},
            "omnibus_sworn_statement": {"correct": 8, "total": 10}}
REGRESSING = {"philgeps_registration": {"correct": 9, "total": 10},
              "omnibus_sworn_statement": {"correct": 10, "total": 10}}
IMPROVED = {"philgeps_registration": {"correct": 10, "total": 10},
            "omnibus_sworn_statement": {"correct": 9, "total": 10}}
FAILING = {"philgeps_registration": {"correct": 5, "total": 10},
           "omnibus_sworn_statement": {"correct": 10, "total": 10}}


def accepted_corrections(conn, tx, n, prefix="c"):
    batch = []
    for i in range(n):
        c = submit_correction(
            conn, tx, verdict_id="verdict-1",
            field_path=f"{prefix}.documents.philgeps_registration.present",
            ai_value=True, correct_value=False,
            reason=f"staff override {i}", submitted_by="staff-01")
        adjudicate_correction(conn, c["correction_id"], "ACCEPTED", "reviewer-01",
                              "confirmed against the physical bid folder")
        batch.append(c["correction_id"])
    return batch


class LearningTests(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.tx = new_tx(self.conn)
        register_benchmark_cases(self.conn, BENCHMARK_CASES, "ml-lead-01")

    def tearDown(self):
        self.conn.close()

    def _batch(self, n=4):
        accepted_corrections(self.conn, self.tx, n)
        return create_training_batch(self.conn, created_by="ml-engineer-01",
                                     base_training_set="original-train-v1")

    def _candidate(self, version, batch_id):
        return register_candidate(
            self.conn, version=version, training_batch_id=batch_id,
            q_lora_config={"base_training_set": "original-train-v1",
                           "replay_ratio": 0.3, "lora_r": 16},
            corpus_snapshot_id="corpus-ra12009-2025-02")

    def _baseline_production(self, batch_id):
        self._candidate("model-v1", batch_id)
        self.assertTrue(evaluate_candidate(self.conn, "model-v1", BASELINE,
                                           "ci-runner-01")["passed"])
        approve_candidate(self.conn, "model-v1", "legal-officer-01", "initial model")
        deploy_candidate(self.conn, "model-v1", "ml-engineer-01", strategy="direct")

    def test_correction_submission_requires_context(self):
        with self.assertRaises(CoreError):
            submit_correction(self.conn, self.tx, verdict_id="v", field_path="",
                              ai_value=True, correct_value=False, reason="why",
                              submitted_by="staff-01")

    def test_submitter_cannot_adjudicate_own_correction(self):
        c = submit_correction(self.conn, self.tx, verdict_id="v",
                              field_path="a.b", ai_value=1, correct_value=2,
                              reason="override", submitted_by="staff-01")
        with self.assertRaises(CoreError):
            adjudicate_correction(self.conn, c["correction_id"], "ACCEPTED",
                                  "staff-01")
        result = adjudicate_correction(self.conn, c["correction_id"], "ACCEPTED",
                                       "reviewer-01")
        self.assertTrue(result["training_eligible"])

    def test_rejected_corrections_never_become_training_data(self):
        for i in range(3):
            c = submit_correction(self.conn, self.tx, verdict_id="v",
                                  field_path=f"reject.{i}", ai_value=1,
                                  correct_value=2, reason="override",
                                  submitted_by="staff-01")
            adjudicate_correction(self.conn, c["correction_id"], "REJECTED",
                                  "reviewer-01", "AI was actually right")
        accepted_corrections(self.conn, self.tx, 3)
        batch = create_training_batch(self.conn, created_by="ml-engineer-01",
                                      base_training_set="original-train-v1")
        self.assertEqual(batch["correction_count"], 3)
        self.assertEqual(len(batch["examples"]), 3)

    def test_batch_requires_minimum_adjudicated_corrections(self):
        accepted_corrections(self.conn, self.tx, 2)
        with self.assertRaises(CoreError):
            create_training_batch(self.conn, created_by="ml-engineer-01",
                                  base_training_set="original-train-v1")

    def test_benchmark_is_frozen(self):
        before = benchmark_snapshot_hash(self.conn)
        with self.assertRaises(Exception):
            self.conn.execute("UPDATE benchmark_case SET expected_json='{}' WHERE case_id='philgeps_registration-0'")
        with self.assertRaises(Exception):
            self.conn.execute("DELETE FROM benchmark_case WHERE case_id='philgeps_registration-0'")
        self.assertEqual(benchmark_snapshot_hash(self.conn), before)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM benchmark_case").fetchone()["c"], 20)

    def test_benchmark_snapshot_hash_changes_when_cases_change(self):
        before = benchmark_snapshot_hash(self.conn)
        register_benchmark_cases(self.conn, [{
            "case_id": "new-1", "slice_name": "financial_proposal",
            "input": {"document": "x"}, "expected": {"decision": "PASS"},
            "source_ref": "historical",
        }], "ml-lead-01")
        self.assertNotEqual(benchmark_snapshot_hash(self.conn), before)

    def test_candidate_requires_replay_of_original_training_set(self):
        batch = self._batch()
        with self.assertRaises(CoreError):
            register_candidate(self.conn, version="bad-v1",
                               training_batch_id=batch["batch_id"],
                               q_lora_config={"lora_r": 16})
        self.assertTrue(self._candidate("model-v1", batch["batch_id"]))

    def test_per_slice_regression_blocks_even_with_higher_overall(self):
        batch = self._batch()
        self._baseline_production(batch["batch_id"])
        self._candidate("model-v2", batch["batch_id"])
        result = evaluate_candidate(self.conn, "model-v2", REGRESSING, "ci-runner-01")
        self.assertGreater(result["overall_score"], 0.90)
        self.assertFalse(result["passed"])
        self.assertTrue(any(r["slice"] == "philgeps_registration"
                            for r in result["regressions"]))

    def test_evaluation_passes_with_no_regression(self):
        batch = self._batch()
        self._baseline_production(batch["batch_id"])
        self._candidate("model-v2", batch["batch_id"])
        result = evaluate_candidate(self.conn, "model-v2", IMPROVED, "ci-runner-01")
        self.assertTrue(result["passed"])
        self.assertEqual(result["regressions"], [])

    def test_human_gate_cannot_be_bypassed(self):
        batch = self._batch()
        self._candidate("model-v1", batch["batch_id"])
        failed = evaluate_candidate(self.conn, "model-v1", FAILING, "ci-runner-01")
        self.assertFalse(failed["passed"])
        with self.assertRaises(CoreError):
            approve_candidate(self.conn, "model-v1", "legal-officer-01")
        with self.assertRaises(CoreError):
            deploy_candidate(self.conn, "model-v1", "ml-engineer-01")

    def test_deploy_requires_approval(self):
        batch = self._batch()
        self._candidate("model-v1", batch["batch_id"])
        self.assertTrue(evaluate_candidate(self.conn, "model-v1", BASELINE,
                                           "ci-runner-01")["passed"])
        with self.assertRaises(CoreError):
            deploy_candidate(self.conn, "model-v1", "ml-engineer-01")

    def test_canary_promotion_requires_a_different_human(self):
        batch = self._batch()
        self._candidate("model-v1", batch["batch_id"])
        evaluate_candidate(self.conn, "model-v1", BASELINE, "ci-runner-01")
        approve_candidate(self.conn, "model-v1", "legal-officer-01")
        deploy_candidate(self.conn, "model-v1", "ml-engineer-01", strategy="canary")
        self.assertIsNone(current_production(self.conn))
        with self.assertRaises(CoreError):
            promote_canary(self.conn, "model-v1", "ml-engineer-01")
        result = promote_canary(self.conn, "model-v1", "bac-chair-01")
        self.assertEqual(result["status"], "PRODUCTION")
        self.assertEqual(current_production(self.conn)["version"], "model-v1")

    def test_rollback_restores_the_previous_model(self):
        batch = self._batch()
        self._baseline_production(batch["batch_id"])
        self._candidate("model-v2", batch["batch_id"])
        evaluate_candidate(self.conn, "model-v2", IMPROVED, "ci-runner-01")
        approve_candidate(self.conn, "model-v2", "legal-officer-01")
        with self.assertRaises(CoreError):
            deploy_candidate(self.conn, "model-v2", "ml-engineer-01", strategy="direct")
        deploy_candidate(self.conn, "model-v2", "ml-engineer-01", strategy="canary")
        promote_canary(self.conn, "model-v2", "bac-chair-01")
        self.assertEqual(current_production(self.conn)["version"], "model-v2")
        result = rollback(self.conn, "model-v1", "ml-engineer-01",
                          "false-positive rate increased in production")
        self.assertEqual(result["production"], "model-v1")
        self.assertEqual(current_production(self.conn)["version"], "model-v1")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM ledger_event WHERE event_type='MODEL_ROLLED_BACK'"
        ).fetchone()["c"], 1)

    def test_citation_feedback_requires_a_real_new_corpus_snapshot(self):
        f = flag_citation(self.conn, self.tx, chunk_id="RA12009-IRR-S34-1",
                          issue="OUTDATED", note="GPPB raised the threshold",
                          submitted_by="staff-01")
        with self.assertRaises(CoreError):
            resolve_citation_feedback(self.conn, f["feedback_id"],
                                      "legal-officer-01", "does-not-exist")
        result = resolve_citation_feedback(self.conn, f["feedback_id"],
                                           "legal-officer-01",
                                           "corpus-ra12009-2025-02")
        self.assertEqual(result["status"], "RESOLVED")

    def test_learning_dashboard_and_provenance(self):
        batch = self._batch()
        self._baseline_production(batch["batch_id"])
        submit_verdict(self.conn, self.tx, sample_verdict(
            resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")))
        prov = verdict_provenance(self.conn, self.tx)
        self.assertTrue(prov["verdicts"])
        self.assertEqual(prov["current_production_model"], "model-v1")
        self.assertIn("corpus_snapshot_id", prov["verdicts"][0])
        board = learning_dashboard(self.conn)
        self.assertEqual(board["benchmark_cases"], 20)
        self.assertEqual(board["current_production"], "model-v1")
        self.assertTrue(board["human_gate_required"])

    def test_learning_tables_survive_migration(self):
        batch = self._batch()
        self._baseline_production(batch["batch_id"])
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "vps.db")
            migrate(self.conn, target)
            tconn = core._connect(target)
            try:
                self.assertEqual(tconn.execute(
                    "SELECT count(*) c FROM benchmark_case").fetchone()["c"], 20)
                self.assertEqual(tconn.execute(
                    "SELECT count(*) c FROM correction").fetchone()["c"], 4)
                self.assertEqual(tconn.execute(
                    "SELECT version FROM model_version WHERE status='PRODUCTION'"
                ).fetchone()["version"], "model-v1")
            finally:
                tconn.close()

    def test_full_loop_end_to_end(self):
        batch = self._batch()
        self._candidate("model-v1", batch["batch_id"])
        evaluation = evaluate_candidate(self.conn, "model-v1", BASELINE, "ci-runner-01")
        self.assertTrue(evaluation["passed"])
        approve_candidate(self.conn, "model-v1", "legal-officer-01", "approved for canary")
        deploy_candidate(self.conn, "model-v1", "ml-engineer-01", strategy="canary")
        promote_canary(self.conn, "model-v1", "bac-chair-01")
        self.assertEqual(current_production(self.conn)["version"], "model-v1")
        types = {r["event_type"] for r in self.conn.execute(
            "SELECT event_type FROM ledger_event")}
        for expected in ("CORRECTION_SUBMITTED", "CORRECTION_ADJUDICATED",
                         "TRAINING_BATCH_CREATED", "CANDIDATE_MODEL_REGISTERED",
                         "CANDIDATE_EVALUATED", "MODEL_APPROVED",
                         "MODEL_DEPLOYED", "MODEL_PROMOTED"):
            self.assertIn(expected, types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
