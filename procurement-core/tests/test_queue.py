"""Tests for the advisory BAC review queue + readiness + adapter scaffolding.

Covered (previously untested modules):
- review_queue.py (enqueue/claim/recommend/resolve/override + RBAC)
- core.submit_verdict auto-enqueue on ABSTAIN/FAIL
- readiness_report.py (fail-closed blockers)
- training_adapter.py (SFT/completion records + loopback stub)
- model_plumbing_check.py (endpoint check + stub)
- review_server.py helpers (loopback, auth, RBAC denial via handle_post)
- model_adapter_server.py (JSON extract + normalize + upstream kinds)

Run:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (
    CoreError, init_db, resolve_rules, create_transaction, submit_verdict,
    sha256_text, request_transition, record_bid_opening, add_signoff,
)
from checker import sample_checklist
from extraction import LocalHTTPBackend, StubBackend, load_schema, valid_stub_response
from learning import submit_correction, adjudicate_correction
from review_queue import (
    enqueue_verdict, list_queue, get_item, claim_item, recommend, resolve_item,
    submit_override_correction, adjudicate_override, queue_dashboard,
    require_role, render_queue_html,
)
from readiness_report import build_report, audit_rules


def new_tx(conn, **kw):
    params = dict(
        regime="RA12009_IRR", posting_date="2026-11-05",
        mode="Competitive Bidding", award_criterion="LCRB",
        drafter_id="staff-01",
        legal_basis_note="Legal officer memo: RA 12009 applies",
    )
    params.update(kw)
    return create_transaction(conn, **params)


def sample_abstain_verdict(conn, tx):
    r = resolve_rules(conn, "2026-11-05", "RA12009_IRR")
    return submit_verdict(conn, tx, dict(
        status="ABSTAIN", decision="ABSTAIN", confidence=0.4,
        missing_documents=[], invalid_documents=[], notes=["uncertain"],
        evidence_spans=[], rule_citations=[],
        model_version="qwen2.5-3b-ft-procurement-stub",
        ruleset_version=r["ruleset_version"],
        corpus_snapshot_id=r["corpus_snapshot_id"],
        prompt_hash=sha256_text("p"),
    ), actor_id="checker")


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        self.tx = new_tx(self.conn)
        self.verdict = sample_abstain_verdict(self.conn, self.tx)

    def tearDown(self):
        self.conn.close()

    def test_submit_verdict_autoenqueues_abstain(self):
        # sample_abstain_verdict above went through submit_verdict, which
        # auto-enqueues advisory items.
        self.assertEqual(list_queue(self.conn)["count"], 1)

    def test_enqueue_idempotent(self):
        v2 = sample_abstain_verdict(self.conn, self.tx)
        item = enqueue_verdict(self.conn, self.tx, v2["verdict_id"],
                               created_by="tester")
        self.assertTrue(item["advisory"])
        again = enqueue_verdict(self.conn, self.tx, v2["verdict_id"],
                                created_by="tester")
        self.assertTrue(again["existing"])

    def test_claim_and_recommend(self):
        item_id = list_queue(self.conn)["items"][0]["id"]
        claimed = claim_item(self.conn, item_id, "bac-01", actor_role="BAC_MEMBER")
        self.assertEqual(claimed["status"], "CLAIMED")
        rec = recommend(self.conn, item_id, "bac-01", "BAC_MEMBER", "NEEDS_HUMAN_REVIEW")
        self.assertEqual(rec["action"], "RECOMMEND_REVIEW")

    def test_rbac_rejects_wrong_roles(self):
        with self.assertRaises(CoreError):
            require_role("RESOLVE", "BAC_STAFF")
        with self.assertRaises(CoreError):
            require_role("REQUEST_CORRECTION", "BAC_MEMBER")
        # admitted roles pass
        require_role("RESOLVE", "BAC_MEMBER")
        require_role("REQUEST_CORRECTION", "BAC_STAFF")

    def test_resolve_by_member(self):
        item_id = list_queue(self.conn)["items"][0]["id"]
        resolve_item(self.conn, item_id, "bac-01", "BAC_MEMBER",
                     "ADVISORY_REVIEW_COMPLETE", note="checked evidence")
        self.assertEqual(get_item(self.conn, item_id)["status"], "RESOLVED")

    def test_override_then_second_reviewer_adjudicates(self):
        item_id = list_queue(self.conn)["items"][0]["id"]
        corr = submit_override_correction(
            self.conn, item_id, field_path="documents.philgeps.valid_until",
            ai_value="2025-01-01", correct_value="2027-01-01",
            reason="misread expiry", submitted_by="staff-01")
        self.assertEqual(corr["status"], "PENDING")
        adjudicated = adjudicate_override(
            self.conn, corr["correction_id"], "ACCEPTED", "reviewer-02",
            actor_role="SECOND_REVIEWER")
        self.assertTrue(adjudicated["training_eligible"])

    def test_submitter_cannot_adjudicate(self):
        item_id = list_queue(self.conn)["items"][0]["id"]
        corr = submit_override_correction(
            self.conn, item_id, field_path="documents.philgeps.valid_until",
            ai_value="2025-01-01", correct_value="2027-01-01",
            reason="misread expiry", submitted_by="staff-01")
        with self.assertRaises(CoreError):
            adjudicate_override(
                self.conn, corr["correction_id"], "ACCEPTED", "staff-01",
                actor_role="SECOND_REVIEWER")

    def test_adjudicate_role_enforced(self):
        # BAC_STAFF may submit corrections but may not adjudicate them.
        item_id = list_queue(self.conn)["items"][0]["id"]
        corr = submit_override_correction(
            self.conn, item_id, field_path="documents.philgeps.valid_until",
            ai_value="2025-01-01", correct_value="2027-01-01",
            reason="misread expiry", submitted_by="staff-01")
        with self.assertRaises(CoreError):
            adjudicate_override(
                self.conn, corr["correction_id"], "ACCEPTED", "someone-else",
                actor_role="BAC_STAFF")

    def test_dashboard_and_html(self):
        dash = queue_dashboard(self.conn)
        self.assertEqual(dash["open_items"], 1)
        html = render_queue_html(self.conn)
        self.assertIn("Advisory only", html)
        self.assertIn("BAC/TWG/HOPE", html)


class ReadinessTests(unittest.TestCase):
    def test_report_flags_blockers(self):
        report = build_report()
        self.assertFalse(report["ready_for_production"])
        self.assertGreater(len(report["blockers"]), 0)
        self.assertTrue(report["advisory_only"])
        self.assertEqual(report["corpus"]["chunks_total"], 14)
        self.assertEqual(len(report["corpus"]["unverified_chunks"]), 1)
        self.assertEqual(report["forms"]["forms_total"], 36)
        self.assertEqual(report["forms"]["official_bodies_registered"], 0)

    def test_audit_rules_counts_unverified(self):
        audit = audit_rules()
        self.assertEqual(audit["rules_total"], 20)
        self.assertEqual(len(audit["rules_unverified"]), 5)


class AdapterScaffoldTests(unittest.TestCase):
    def test_training_adapter_sft_record_valid(self):
        from training_adapter import build_sft_record, build_completion_record
        schema = load_schema()
        pages = [{"page": 1, "text": "PhilGEPS Platinum, valid until 2027-06-30"}]
        target = valid_stub_response()
        record = build_sft_record(pages=pages, target_response=target, schema=schema,
                                  model_version="test")
        self.assertIn("messages", record)
        self.assertTrue(record["metadata"]["target_valid"])
        completion = build_completion_record(pages=pages, target_response=target, schema=schema)
        self.assertTrue(completion["metadata"]["target_valid"])

    def test_training_stub_roundtrip(self):
        import threading
        from http.server import ThreadingHTTPServer
        from training_adapter import _StubHandler

        handler = type("Handler", (_StubHandler,), {"responder": None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = LocalHTTPBackend(
                url=f"http://127.0.0.1:{port}/extract", model_version="stub-test")
            out = backend.extract(
                [{"page": 1, "text": "PhilGEPS Platinum"}], load_schema())
            self.assertEqual(out["model_version"], "stub-test")
        finally:
            server.shutdown()
            server.server_close()

    def test_plumbing_check_against_stub(self):
        from model_plumbing_check import check_endpoint
        import threading  # noqa: F401
        from http.server import ThreadingHTTPServer
        from training_adapter import _StubHandler

        handler = type("Handler", (_StubHandler,), {"responder": None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            summary = check_endpoint(f"http://127.0.0.1:{port}/extract")
            self.assertEqual(summary["transport_ok"], 2)
            self.assertEqual(summary["schema_valid_rate"], 1.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_adapter_json_extract_and_normalize(self):
        from model_adapter_server import _extract_json, normalize_response
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(_extract_json('```json\n{"b": 2}\n```'), {"b": 2})
        self.assertIsNone(_extract_json("no json here"))
        fixed = normalize_response({"documents": {}}, model_version="m1")
        self.assertEqual(fixed["model_version"], "m1")

    def test_adapter_refuses_non_loopback(self):
        from model_adapter_server import _assert_loopback
        with self.assertRaises(CoreError):
            _assert_loopback("https://api.openai.example/v1", "upstream")
        _assert_loopback("http://127.0.0.1:11434/api/chat", "upstream")

    def test_review_server_auth_helpers(self):
        from review_server import _assert_loopback, _check_auth
        from review_server import _query_authed
        _assert_loopback("127.0.0.1")
        with self.assertRaises(CoreError):
            _assert_loopback("0.0.0.0")
        self.assertFalse(_check_auth({}, None))  # no token configured → open
        class H(dict):
            def get(self, k, d=""):
                return super().get(k, d)
        self.assertTrue(_check_auth(H(), "secret"))
        self.assertFalse(_check_auth(H({"X-Auth-Token": "secret"}), "secret"))
        self.assertTrue(_query_authed({"token": "secret"}, "secret"))
        self.assertFalse(_query_authed({"token": "wrong"}, "secret"))


if __name__ == "__main__":
    unittest.main()
