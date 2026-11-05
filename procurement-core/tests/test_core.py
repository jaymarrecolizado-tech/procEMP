"""Acceptance tests A1-A15 from 03-tech-spec-rules-evidence-gate.md.

Run:  python -m unittest discover -s tests -v
"""

import os
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
from core import (
    CoreError, GateBlocked, PolicyBlocked, RuleResolutionError, UnverifiedRuleError,
    canonical, init_db, suggest_regime, resolve_rules, get_rule,
    create_transaction, get_tx, ingest_document, record_bid_opening,
    record_financial_opening, add_signoff, request_transition, submit_verdict,
    export_folder, verify_chain, migrate, issue_number, sha256_text,
)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
