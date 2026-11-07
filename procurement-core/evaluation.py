"""Frozen-slice evaluation + confidence calibration harness (Track A).

Runs an extraction backend against frozen benchmark cases (registered via
learning.register_benchmark_cases) and reports:
- per-slice schema-valid rate and field accuracy vs expected
- abstention-safety: unreadable/sparse slices must NOT produce confident
  false fields (validator fail-close is exercised end-to-end)
- confidence calibration buckets (how often confidence bands match accuracy)

Results are fed to learning.evaluate_candidate() which enforces per-slice
non-regression + absolute thresholds behind the human approval gate. This
module never approves or deploys a model.

Compatible backends: StubBackend (tests/dry-run), LocalHTTPBackend
(loopback model server), or any ExtractionBackend. Use only approved,
non-confidential samples. All inference stays loopback/local.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

from core import _connect, canonical, init_db, now
from dataset import build_benchmark_cases
from extraction import ExtractionError, StubBackend, load_schema, valid_stub_response, validate_output
from learning import benchmark_snapshot_hash, evaluate_candidate, register_benchmark_cases


def _expected_fields(expected):
    """Flatten expected target into {(doc, field): value}."""
    flat = {}
    for doc_key, doc in (expected.get("documents") or {}).items():
        if doc.get("status") == "UNREADABLE":
            flat[(doc_key, "__status")] = "UNREADABLE"
            continue
        for fname, value in (doc.get("fields") or {}).items():
            flat[(doc_key, fname)] = value
    return flat


def _predicted_fields(validation):
    flat = {}
    for doc_key, doc in validation.documents.items():
        if doc == {"status": "UNREADABLE"}:
            flat[(doc_key, "__status")] = "UNREADABLE"
            continue
        for fname, value in doc.items():
            flat[(doc_key, fname)] = value
    return flat


def _compare(expected, validation):
    """Field-level comparison. Returns (correct, total)."""
    exp = _expected_fields(expected)
    pred = _predicted_fields(validation)
    correct = sum(1 for k, v in exp.items() if pred.get(k) == v)
    return correct, len(exp)


def run_backend_eval(backend, benchmark_cases, schema=None, model_version=None):
    """Evaluate backend against benchmark cases. Returns summary dict.

    benchmark_cases: list in learning.register_benchmark_cases shape:
      {case_id, slice_name, input: {pages}, expected, source_ref}
    """
    schema = schema or load_schema()
    per_case = []
    latency_ms = []
    for case in benchmark_cases:
        pages = (case.get("input") or {}).get("pages") or []
        expected = case.get("expected") or {}
        case_id = case.get("case_id", "?")
        slice_name = case.get("slice_name", "?")
        t0 = time.time()
        row = {
            "case_id": case_id, "slice_name": slice_name,
            "pages": len(pages), "ok": False, "schema_valid": False,
            "violations": [], "uncertain_fields": [],
            "field_correct": 0, "field_total": 0,
            "field_accuracy": None, "latency_ms": None, "error": None,
            "avg_confidence": None,
        }
        try:
            raw = backend.extract(pages, schema)
            row["latency_ms"] = round((time.time() - t0) * 1000, 1)
            latency_ms.append(row["latency_ms"])
            validation = validate_output(raw, schema, pages)
            row["ok"] = True
            row["schema_valid"] = validation.valid
            row["violations"] = validation.violations
            row["uncertain_fields"] = validation.uncertain_fields
            correct, total = _compare(expected, validation)
            row["field_correct"] = correct
            row["field_total"] = total
            row["field_accuracy"] = round(correct / total, 4) if total else None
            # mean confidence over predicted clean fields (advisory signal)
            confs = []
            for doc in (raw.get("documents") or {}).values():
                if isinstance(doc, dict):
                    confs += list((doc.get("confidence") or {}).values())
            row["avg_confidence"] = round(statistics.mean(confs), 4) if confs else None
        except Exception as exc:  # ExtractionError or transport error
            row["latency_ms"] = round((time.time() - t0) * 1000, 1)
            row["error"] = str(exc)
        per_case.append(row)

    per_slice = {}
    for row in per_case:
        s = per_slice.setdefault(row["slice_name"], {"correct": 0, "total": 0, "cases": 0,
                                                      "schema_valid": 0, "transport_ok": 0})
        s["correct"] += row["field_correct"]
        s["total"] += row["field_total"]
        s["cases"] += 1
        s["schema_valid"] += int(row["schema_valid"])
        s["transport_ok"] += int(row["ok"])
    for s in per_slice.values():
        s["field_accuracy"] = round(s["correct"] / s["total"], 4) if s["total"] else 0.0
        s["schema_valid_rate"] = round(s["schema_valid"] / s["cases"], 4) if s["cases"] else 0.0

    total_cases = len(per_case)
    schema_valid_cases = sum(1 for r in per_case if r["schema_valid"])
    calibration = _calibration_buckets(per_case)
    return {
        "evaluated_at": now(),
        "model_version": model_version or getattr(backend, "model_version", "unknown"),
        "benchmark_cases": total_cases,
        "transport_ok": sum(1 for r in per_case if r["ok"]),
        "schema_valid_cases": schema_valid_cases,
        "schema_valid_rate": round(schema_valid_cases / total_cases, 4) if total_cases else 0.0,
        "field_correct": sum(r["field_correct"] for r in per_case),
        "field_total": sum(r["field_total"] for r in per_case),
        "field_accuracy": round(
            sum(r["field_correct"] for r in per_case) / sum(r["field_total"] for r in per_case), 4
        ) if sum(r["field_total"] for r in per_case) else 0.0,
        "avg_latency_ms": round(statistics.mean(latency_ms), 1) if latency_ms else None,
        "per_slice": {k: {"correct": v["correct"], "total": v["total"],
                           "field_accuracy": v["field_accuracy"],
                           "schema_valid_rate": v["schema_valid_rate"],
                           "cases": v["cases"]} for k, v in per_slice.items()},
        "calibration": calibration,
        "advisory_only": True,
        "human_gate": "evaluate_candidate() + approve_candidate() required before any deployment",
        "results": per_case,
    }


def _calibration_buckets(per_case):
    """Compare mean confidence vs field accuracy per confidence band."""
    buckets = {}
    for row in per_case:
        if row["field_total"] == 0 or row["avg_confidence"] is None:
            continue
        conf = row["avg_confidence"]
        if conf >= 0.9:
            band = "0.90-1.00"
        elif conf >= 0.75:
            band = "0.75-0.90"
        elif conf >= 0.5:
            band = "0.50-0.75"
        else:
            band = "0.00-0.50"
        b = buckets.setdefault(band, {"cases": 0, "correct": 0, "total": 0})
        b["cases"] += 1
        b["correct"] += row["field_correct"]
        b["total"] += row["field_total"]
    for b in buckets.values():
        b["accuracy"] = round(b["correct"] / b["total"], 4) if b["total"] else 0.0
    return buckets


def candidate_results(summary):
    """Convert run_backend_eval summary into learning.evaluate_candidate results shape."""
    return {
        slice_name: {"correct": s["correct"], "total": s["total"]}
        for slice_name, s in summary["per_slice"].items()
    }


def evaluate_registered_candidate(conn, version, backend, *, evaluator, schema=None):
    """Run backend against the FROZEN benchmark in DB, then evaluate_candidate.

    Human approval (approve_candidate/deploy/promote) remains a separate,
    explicit step — this function never approves or deploys.
    """
    cases = [dict(r) for r in conn.execute(
        "SELECT case_id, slice_name, input_json, expected_json FROM benchmark_case ORDER BY case_id"
    ).fetchall()]
    benchmark = []
    for r in cases:
        benchmark.append({
            "case_id": r["case_id"], "slice_name": r["slice_name"],
            "input": json.loads(r["input_json"]), "expected": json.loads(r["expected_json"]),
        })
    if not benchmark:
        raise ValueError("no frozen benchmark cases registered; register_benchmark_cases first")
    summary = run_backend_eval(backend, benchmark, schema=schema)
    decision = evaluate_candidate(conn, version, candidate_results(summary), evaluator)
    return {"summary": summary, "decision": decision,
            "benchmark_snapshot_hash": benchmark_snapshot_hash(conn)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Frozen-slice extraction evaluation (advisory; human gate still required)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stub", help="Dry-run eval with StubBackend over synthetic benchmark")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("registered", help="Evaluate a registered candidate against frozen benchmark in DB")
    p.add_argument("--db", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--evaluator", required=True)
    p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "stub":
        backend = StubBackend(valid_stub_response("qwen2.5-3b-ft-procurement-stub"))
        summary = run_backend_eval(backend, build_benchmark_cases(frozen_by="dry-run"))
        out = {k: v for k, v in summary.items() if k != "results"}
        print(json.dumps(summary if args.json else out, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "registered":
        conn = _connect(args.db)
        try:
            from extraction import LocalHTTPBackend
            # Registered eval uses the candidate's loopback endpoint if provided
            # via CANDIDATE_URL env; default is stub for offline CI.
            import os
            url = os.environ.get("CANDIDATE_URL")
            if url:
                backend = LocalHTTPBackend(url=url, model_version=args.version)
            else:
                backend = StubBackend(valid_stub_response(args.version))
            result = evaluate_registered_candidate(conn, args.version, backend, evaluator=args.evaluator)
            print(json.dumps(result if args.json else result["decision"], indent=2, ensure_ascii=False))
            return 0
        finally:
            conn.close()
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
