"""Tests for Track A/B/Next deliverables:

- checklist_builder.py (mode coverage, fail-closed)
- dataset.py (synthetic plan, slices, provenance)
- evaluation.py (frozen-slice eval, calibration, candidate bridge)
- training_job.py (human-gated orchestration, no auto-deploy)
- ops_backup.py (backup/verify/restore-drill/retention)

All deterministic, stdlib-only, no network.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import CoreError, init_db, create_transaction, resolve_rules
from checker import evaluate_submission
from extraction import StubBackend, valid_stub_response, load_schema
from checklist_builder import build_checklist, supported_modes, validate_checklist_for_checker
from dataset import build_benchmark_cases, build_sft_records, provenance_summary, DATASET_PLAN_VERSION
from evaluation import run_backend_eval, candidate_results, evaluate_registered_candidate
from training_job import plan_training, register_weights_and_eval, QLORA_DEFAULTS
from learning import (
    submit_correction, adjudicate_correction, register_benchmark_cases,
    create_training_batch, register_candidate, evaluate_candidate,
    approve_candidate, current_production,
)
from ops_backup import backup_db, verify_backup, restore_drill, retention_report, RETENTION_POLICY


def new_tx(conn, **kw):
    params = dict(
        regime="RA12009_IRR", posting_date="2026-11-05",
        mode="Competitive Bidding", award_criterion="LCRB",
        drafter_id="staff-01",
        legal_basis_note="Legal officer memo: RA 12009 applies",
    )
    params.update(kw)
    return create_transaction(conn, **params)


class ChecklistBuilderTests(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_competitive_checklist_builds_and_evaluates(self):
        tx = new_tx(self.conn)
        checklist = build_checklist(self.conn, tx, approved_by="bac-secretariat-01", abc=1_000_000)
        self.assertEqual(checklist["mode"], "Competitive Bidding")
        self.assertEqual(len(checklist["documents"]), 4)
        self.assertTrue(checklist["template_only"])
        # bridges into checker: no documents → no evidence spans → ABSTAIN
        # (fail-closed; submit_verdict downgrades evidence-less verdicts)
        verdict = evaluate_submission(
            self.conn, tx, checklist,
            {"as_of": "2026-11-05", "documents": {}})
        self.assertEqual(verdict["decision"], "ABSTAIN")

    def test_svp_base_has_three_documents(self):
        tx = new_tx(self.conn, mode="Small Value Procurement")
        checklist = build_checklist(self.conn, tx, approved_by="bac-secretariat-01")
        self.assertEqual(len(checklist["documents"]), 3)
        with_fin = build_checklist(
            self.conn, tx, approved_by="bac-secretariat-01",
            abc=2_000_000, include_financial_for_svp=True)
        self.assertEqual(len(with_fin["documents"]), 4)

    def test_legacy_regime_fails_closed(self):
        legacy = new_tx(self.conn, regime="RA9184_2016IRR", posting_date="2023-06-01")
        with self.assertRaises(Exception):
            build_checklist(self.conn, legacy, approved_by="bac-secretariat-01")

    def test_unknown_mode_fails_closed(self):
        tx = new_tx(self.conn, mode="Direct Sales")
        with self.assertRaises(CoreError):
            build_checklist(self.conn, tx, approved_by="bac-secretariat-01")

    def test_approved_by_and_abc_required(self):
        tx = new_tx(self.conn)
        with self.assertRaises(CoreError):
            build_checklist(self.conn, tx, approved_by="")
        with self.assertRaises(CoreError):
            build_checklist(self.conn, tx, approved_by="bac-secretariat-01")

    def test_supported_modes(self):
        self.assertIn("Competitive Bidding", supported_modes())
        self.assertIn("Small Value Procurement", supported_modes())


class DatasetTests(unittest.TestCase):
    def test_provenance_is_synthetic_only(self):
        prov = provenance_summary()
        self.assertTrue(prov["synthetic_only"])
        self.assertFalse(prov["confidential_data_used"])
        self.assertFalse(prov["real_training_approved"])
        self.assertIn("clean_digital", prov["slices"])
        self.assertIn("unreadable", prov["slices"])

    def test_benchmark_shape(self):
        cases = build_benchmark_cases(frozen_by="test-freezer")
        self.assertEqual(len(cases), 5)
        for case in cases:
            for key in ("case_id", "slice_name", "input", "expected", "source_ref"):
                self.assertIn(key, case)
        self.assertIn("synthetic", cases[0]["source_ref"])

    def test_sft_records_schema_valid(self):
        records = build_sft_records()
        self.assertEqual(len(records), 5)
        for record in records:
            self.assertTrue(record["metadata"]["target_schema_valid"])
            self.assertTrue(record["metadata"]["synthetic"])


class EvaluationTests(unittest.TestCase):
    def test_stub_eval_scores(self):
        backend = StubBackend(valid_stub_response("stub-eval"))
        summary = run_backend_eval(backend, build_benchmark_cases(frozen_by="x"))
        self.assertEqual(summary["benchmark_cases"], 5)
        self.assertEqual(summary["transport_ok"], 5)
        self.assertEqual(summary["schema_valid_rate"], 1.0)
        self.assertIn("clean_digital", summary["per_slice"])
        self.assertIn("calibration", summary)

    def test_candidate_results_bridge(self):
        backend = StubBackend(valid_stub_response("m"))
        summary = run_backend_eval(backend, build_benchmark_cases(frozen_by="x"))
        results = candidate_results(summary)
        for slice_name, r in results.items():
            self.assertIn("correct", r)
            self.assertIn("total", r)

    def test_evaluate_registered_candidate_human_gate(self):
        conn = init_db(":memory:")
        try:
            register_benchmark_cases(
                conn, build_benchmark_cases(frozen_by="bench-keeper"), "bench-keeper")
            # seed 3 adjudicated corrections for a training batch
            tx = new_tx(conn)
            r = resolve_rules(conn, "2026-11-05", "RA12009_IRR")
            from core import submit_verdict, sha256_text
            verdict = submit_verdict(conn, tx, dict(
                status="PASS", decision="PASS", confidence=0.99,
                missing_documents=[], invalid_documents=[], notes=[],
                evidence_spans=[{"field": "x", "page": 1}],
                rule_citations=[{"section": "IRR RA 12009 Sec. 59.1", "verified": True}],
                model_version="m", ruleset_version=r["ruleset_version"],
                corpus_snapshot_id=r["corpus_snapshot_id"],
                prompt_hash=sha256_text("p")), actor_id="checker")
            for i in range(3):
                corr = submit_correction(
                    conn, tx, verdict_id=verdict["verdict_id"],
                    field_path=f"documents.financial_proposal.amount",
                    ai_value=900000, correct_value=950000 + i,
                    reason=f"misread {i}", submitted_by="staff-01")
                adjudicate_correction(conn, corr["correction_id"], "ACCEPTED",
                                      "reviewer-02")
            batch = create_training_batch(conn, created_by="ml-01",
                                          base_training_set="base-synthetic-v1")
            register_candidate(
                conn, version="candidate-eval-1", training_batch_id=batch["batch_id"],
                q_lora_config={"base_training_set": "base-synthetic-v1",
                               "replay_ratio": 0.5})
            backend = StubBackend(valid_stub_response("candidate-eval-1"))
            result = evaluate_registered_candidate(
                conn, "candidate-eval-1", backend, evaluator="evaluator-01")
            self.assertIn("passed", result["decision"])
            # still CANDIDATE: approval is a separate human step
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM model_version WHERE version=? AND status='PRODUCTION'",
                ("candidate-eval-1",)).fetchone())
        finally:
            conn.close()


class TrainingJobTests(unittest.TestCase):
    def test_plan_requires_corrections_and_never_deploys(self):
        conn = init_db(":memory:")
        try:
            tx = new_tx(conn)
            with self.assertRaises(CoreError):
                plan_training(conn, created_by="ml-01", version="v-no-data",
                              out_dir=tempfile.mkdtemp())
            # adjudicate 3 corrections then plan succeeds as CANDIDATE only
            r = resolve_rules(conn, "2026-11-05", "RA12009_IRR")
            from core import submit_verdict, sha256_text
            verdict = submit_verdict(conn, tx, dict(
                status="PASS", decision="PASS", confidence=0.99,
                missing_documents=[], invalid_documents=[], notes=[],
                evidence_spans=[{"field": "x", "page": 1}],
                rule_citations=[{"section": "IRR RA 12009 Sec. 59.1", "verified": True}],
                model_version="m", ruleset_version=r["ruleset_version"],
                corpus_snapshot_id=r["corpus_snapshot_id"],
                prompt_hash=sha256_text("p")), actor_id="checker")
            for i in range(3):
                corr = submit_correction(
                    conn, tx, verdict_id=verdict["verdict_id"],
                    field_path="documents.financial_proposal.amount",
                    ai_value=900000, correct_value=950000 + i,
                    reason=f"misread {i}", submitted_by="staff-01")
                adjudicate_correction(conn, corr["correction_id"], "ACCEPTED",
                                      "reviewer-02")
            plan = plan_training(conn, created_by="ml-01", version="v1-candidate",
                                 out_dir=tempfile.mkdtemp())
            self.assertEqual(plan["status"], "CANDIDATE")
            self.assertEqual(plan["correction_count"], 3)
            self.assertTrue(os.path.exists(plan["dataset_path"]))
            self.assertIsNone(current_production(conn))
        finally:
            conn.close()

    def test_qlora_defaults_carry_replay(self):
        self.assertEqual(QLORA_DEFAULTS["base_model"], "Qwen2.5-3B")
        self.assertIn("rules", QLORA_DEFAULTS["notes"].lower())


class OpsBackupTests(unittest.TestCase):
    def test_backup_verify_restore_drill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "procurement.db")
            conn = init_db(db)
            new_tx(conn)
            conn.close()

            backup_path = os.path.join(tmp, "backup.db")
            manifest = backup_db(db, backup_path, actor="test")
            self.assertTrue(manifest["chain_ok"])
            self.assertTrue(os.path.exists(manifest["manifest_path"]))

            report = verify_backup(backup_path)
            self.assertTrue(report["ok"])
            self.assertGreaterEqual(report["events_total"], 1)

            restored = os.path.join(tmp, "restored.db")
            drill = restore_drill(backup_path, restored)
            self.assertTrue(drill["ok"])
            self.assertTrue(os.path.exists(restored))

    def test_retention_report_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "procurement.db")
            conn = init_db(db)
            new_tx(conn)
            conn.close()
            report = retention_report(db)
            self.assertIn("policy", report)
            self.assertIn("DRAFT", report["policy"]["status"])


if __name__ == "__main__":
    unittest.main()
