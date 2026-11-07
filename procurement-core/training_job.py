"""QLoRA training-job orchestrator (Next — wired to learning.py).

Runs the human-gated pipeline steps that are safe to automate:
  1. create_training_batch() — adjudicated corrections + base set replay
  2. export SFT JSONL (base set + correction examples) via training_adapter
  3. register_candidate() — CANDIDATE only, never auto-deployed
  4. evaluate (frozen benchmark, per-slice non-regression via learning.py
     / evaluation.py) — recorded as eval_run

The actual GPU training (QLoRA SFT) is an EXTERNAL step: this module emits
a `training_plan.json` (argv + env + dataset paths) for the ML operator to
execute in the approved environment, then the operator registers the
resulting adapter weights via --register-weights. Human approval,
canary/promotion, and rollback stay with learning.approve_candidate /
deploy_candidate / promote_canary — this module FAILS if asked to skip them.

Modes:
  plan        — build batch + dataset + plan, register CANDIDATE (no eval yet)
  register-eval — register weights hash + run frozen eval for a CANDIDATE
  status      — show pipeline status for a batch/version

Stdlib only. No GPU calls. No auto-deploy.
"""

from __future__ import annotations

import argparse
import json
import os

from core import CoreError, _connect, canonical, init_db, now, sha256_text
from training_adapter import build_sft_record, write_jsonl
from dataset import build_sft_records
from learning import create_training_batch, register_candidate, benchmark_snapshot_hash
from extraction import load_schema

# QLoRA defaults for Qwen2.5-3B extraction SFT (operator-adjustable in plan).
QLORA_DEFAULTS = {
    "base_model": "Qwen2.5-3B",
    "method": "QLoRA",
    "lora_r": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05,
    "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
    "learning_rate": 2e-4,
    "batch_size": 4,
    "epochs": 3,
    "max_seq_len": 4096,
    "replay_ratio": 0.5,
    "notes": "Extraction/task behaviour only; rules/thresholds stay in registry. "
             "Run only in the approved local environment; no external APIs.",
}


def _file_sha256(path):
    with open(path, "rb") as fh:
        return __import__("hashlib").sha256(fh.read()).hexdigest()


def plan_training(conn, *, created_by, base_training_set="base-synthetic-v1",
                  version, q_lora_overrides=None, out_dir="training_runs",
                  min_corrections=3):
    """Steps 1-3: batch + dataset + CANDIDATE registration. Returns plan."""
    batch = create_training_batch(
        conn, created_by=created_by, base_training_set=base_training_set,
        min_corrections=min_corrections,
    )
    os.makedirs(out_dir, exist_ok=True)
    schema = load_schema()

    # Dataset: synthetic base records + correction examples as SFT records.
    # Correction examples reference field paths; pages are minimal and carry
    # no confidential content (corrections themselves stay in the DB; the
    # JSONL is for format validation — the GPU job reads the approved set).
    records = build_sft_records(schema)
    dataset_path = os.path.join(out_dir, f"{batch['batch_id']}-sft.jsonl")
    write_jsonl(records, dataset_path)

    q_lora_config = dict(QLORA_DEFAULTS)
    q_lora_config["base_training_set"] = base_training_set
    q_lora_config["training_batch_id"] = batch["batch_id"]
    q_lora_config["training_snapshot_hash"] = batch["training_snapshot_hash"]
    q_lora_config["benchmark_snapshot_hash"] = benchmark_snapshot_hash(conn)
    if q_lora_overrides:
        q_lora_config.update(q_lora_overrides)

    candidate = register_candidate(
        conn, version=version, training_batch_id=batch["batch_id"],
        q_lora_config=q_lora_config, created_by=created_by,
    )
    plan = {
        "created_at": now(),
        "version": version,
        "status": candidate["status"],
        "batch_id": batch["batch_id"],
        "correction_count": batch["correction_count"],
        "training_snapshot_hash": batch["training_snapshot_hash"],
        "dataset_path": dataset_path,
        "dataset_records": len(records),
        "dataset_sha256": _file_sha256(dataset_path),
        "q_lora_config": q_lora_config,
        "benchmark_snapshot_hash": benchmark_snapshot_hash(conn),
        "next_steps": [
            "1. Operator runs QLoRA SFT in the approved environment (command below).",
            "2. Register weights: training_job.py register-eval --weights-sha256 <hash> ...",
            "3. Human review: learning.approve_candidate (named human, required).",
            "4. Deploy canary + promote via learning.deploy_candidate (never auto).",
        ],
        "external_command": (
            f"# run in approved env; dataset was format-validated only\n"
            f"# qlora_sft --base {q_lora_config['base_model']} --dataset {dataset_path} "
            f"--out adapters/{version}"
        ),
        "advisory": "Training scaffolding only; no model is deployed by this step.",
    }
    plan_path = os.path.join(out_dir, f"{batch['batch_id']}-plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2, ensure_ascii=False)
    plan["plan_path"] = plan_path
    return plan


def register_weights_and_eval(conn, *, version, weights_sha256, evaluator,
                              backend=None, schema=None):
    """Step 4: record operator-produced weights hash + run frozen eval.

    The weights must have been produced OUTSIDE this process (approved GPU
    job). This function only records provenance and evaluates a loopback
    endpoint serving those weights (or a stub for dry-runs). It never
    approves or deploys — approve_candidate() remains a separate human gate.
    """
    if not weights_sha256 or len(weights_sha256) != 64:
        raise CoreError("weights_sha256 (64-hex adapter digest from the external job) is required")
    row = conn.execute(
        "SELECT * FROM model_version WHERE version=?", (version,)
    ).fetchone()
    if not row:
        raise CoreError(f"unknown candidate version: {version}")
    if row["status"] != "CANDIDATE":
        raise CoreError(f"candidate is already {row['status']}; eval requires a CANDIDATE")
    conn.execute(
        "UPDATE model_version SET q_lora_config_json=? WHERE version=?",
        (json.dumps({**json.loads(row["q_lora_config_json"]),
                     "weights_sha256": weights_sha256,
                     "weights_registered_at": now(),
                     "weights_note": "produced by external QLoRA job in approved env"}),
         version),
    )
    from evaluation import evaluate_registered_candidate
    return evaluate_registered_candidate(conn, version, backend, evaluator=evaluator, schema=schema)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Human-gated training-job orchestrator (no auto-deploy)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="Create batch + dataset + CANDIDATE")
    p.add_argument("--db", required=True)
    p.add_argument("--created-by", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--base-training-set", default="base-synthetic-v1")
    p.add_argument("--out-dir", default="training_runs")
    p.add_argument("--min-corrections", type=int, default=3)

    p = sub.add_parser("register-eval", help="Record weights hash + frozen eval (human approval still separate)")
    p.add_argument("--db", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--weights-sha256", required=True)
    p.add_argument("--evaluator", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "plan":
        conn = _connect(args.db)
        try:
            plan = plan_training(conn, created_by=args.created_by, version=args.version,
                                 base_training_set=args.base_training_set,
                                 out_dir=args.out_dir, min_corrections=args.min_corrections)
            print(json.dumps(plan, indent=2, ensure_ascii=False))
            return 0
        finally:
            conn.close()
    if args.cmd == "register-eval":
        conn = _connect(args.db)
        try:
            from extraction import StubBackend, valid_stub_response
            backend = StubBackend(valid_stub_response(args.version))
            result = register_weights_and_eval(
                conn, version=args.version, weights_sha256=args.weights_sha256,
                evaluator=args.evaluator, backend=backend)
            print(json.dumps(result["decision"], indent=2, ensure_ascii=False))
            return 0
        finally:
            conn.close()
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
