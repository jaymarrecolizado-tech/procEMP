"""Step 10: correction logging + human-gated continuous fine-tuning.

"Self-improving" has a bounded meaning here: corrections improve the model over
time, but NO version ever reaches production without a named human approving it.
An unreviewed model update silently changing how documents get flagged is an
audit risk, not just a technical one.

Loop:
  submit correction -> second reviewer adjudicates -> accepted corrections form
  a training batch (with the original training set replayed) -> candidate model
  -> evaluated on a FROZEN benchmark with PER-SLICE non-regression -> human
  review gate -> canary -> human promotion -> versioned production with rollback

Every verdict already logs model_version + corpus_snapshot_id + ruleset_version,
so a decision made months ago can be traced to the exact model that produced it.
"""

from __future__ import annotations

import json

from core import CoreError, append_event, canonical, now, sha256_text, uid

MIN_CORRECTIONS_FOR_BATCH = 3
MIN_ABSOLUTE_SCORE = 0.90
MIN_ABSOLUTE_SLICE_SCORE = 0.80
REGRESSION_TOLERANCE = 0.0

VALID_ADJUDICATIONS = ("ACCEPTED", "REJECTED")
VALID_CITATION_ISSUES = ("WRONG", "OUTDATED", "IRRELEVANT")
VALID_DEPLOY_STRATEGIES = ("canary", "direct")


def _j(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse(text, default=None):
    if not text:
        return default
    return json.loads(text)


def _row(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- corrections

def submit_correction(conn, tx_id, *, verdict_id, field_path, ai_value,
                      correct_value, reason, submitted_by):
    """BAC staff override of an AI verdict. NOT training data until adjudicated."""
    for label, value in (("field_path", field_path), ("reason", reason),
                         ("submitted_by", submitted_by)):
        if not value:
            raise CoreError(f"{label} is required")
    if ai_value is None or correct_value is None:
        raise CoreError("both ai_value and correct_value are required")
    cid = uid()
    conn.execute(
        "INSERT INTO correction (id, tx_id, verdict_id, field_path, ai_value, "
        "correct_value, reason, submitted_by, submitted_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,'PENDING')",
        (cid, tx_id, verdict_id, field_path, _j(ai_value), _j(correct_value),
         reason, submitted_by, now()),
    )
    append_event(conn, tx_id, "CORRECTION_SUBMITTED", submitted_by, "BAC_STAFF", {
        "correction_id": cid, "verdict_id": verdict_id,
        "field_path": field_path, "status": "PENDING",
        "note": "not training data until a second reviewer adjudicates",
    })
    return {"correction_id": cid, "status": "PENDING"}


def adjudicate_correction(conn, correction_id, decision, adjudicator, note=""):
    """A staff override can itself be wrong, or gamed. Second reviewer required."""
    if decision not in VALID_ADJUDICATIONS:
        raise CoreError(f"decision must be one of {VALID_ADJUDICATIONS}")
    if not adjudicator:
        raise CoreError("adjudicator (a named second reviewer) is required")
    row = _row(conn, "SELECT * FROM correction WHERE id=?", (correction_id,))
    if not row:
        raise CoreError("unknown correction")
    if row["status"] != "PENDING":
        raise CoreError(f"correction already {row['status']}")
    if adjudicator == row["submitted_by"]:
        raise CoreError("segregation of duties: the submitter cannot adjudicate their own correction")
    conn.execute(
        "UPDATE correction SET status=?, adjudicated_by=?, adjudicated_at=?, "
        "adjudication_note=? WHERE id=?",
        (decision, adjudicator, now(), note, correction_id),
    )
    append_event(conn, row["tx_id"], "CORRECTION_ADJUDICATED", adjudicator,
                 "SECOND_REVIEWER", {
                     "correction_id": correction_id, "decision": decision,
                     "submitted_by": row["submitted_by"], "note": note,
                 })
    return {"correction_id": correction_id, "status": decision,
            "training_eligible": decision == "ACCEPTED"}


def flag_citation(conn, tx_id, *, chunk_id, issue, note, submitted_by):
    if issue not in VALID_CITATION_ISSUES:
        raise CoreError(f"issue must be one of {VALID_CITATION_ISSUES}")
    if not submitted_by or not chunk_id:
        raise CoreError("chunk_id and submitted_by are required")
    fid = uid()
    conn.execute(
        "INSERT INTO citation_feedback (id, tx_id, chunk_id, issue, note, "
        "submitted_by, submitted_at, status) VALUES (?,?,?,?,?,?,?,'OPEN')",
        (fid, tx_id, chunk_id, issue, note, submitted_by, now()),
    )
    append_event(conn, tx_id, "CITATION_FEEDBACK_SUBMITTED", submitted_by,
                 "BAC_STAFF", {"feedback_id": fid, "chunk_id": chunk_id,
                               "issue": issue, "status": "OPEN"})
    return {"feedback_id": fid, "status": "OPEN"}


def resolve_citation_feedback(conn, feedback_id, resolved_by, resolved_snapshot_id):
    """A citation issue is only resolved when a NEW approved corpus snapshot exists."""
    row = _row(conn, "SELECT * FROM citation_feedback WHERE id=?", (feedback_id,))
    if not row:
        raise CoreError("unknown citation feedback")
    if row["status"] != "OPEN":
        raise CoreError(f"feedback already {row['status']}")
    if not resolved_by or not resolved_snapshot_id:
        raise CoreError("resolved_by and resolved_snapshot_id are required")
    snap = _row(conn, "SELECT * FROM corpus_snapshot WHERE id=?", (resolved_snapshot_id,))
    if not snap:
        raise CoreError("resolved_snapshot_id must reference a real corpus snapshot")
    conn.execute(
        "UPDATE citation_feedback SET status='RESOLVED', resolved_by=?, "
        "resolved_at=?, resolved_snapshot_id=? WHERE id=?",
        (resolved_by, now(), resolved_snapshot_id, feedback_id),
    )
    append_event(conn, row["tx_id"], "CITATION_FEEDBACK_RESOLVED", resolved_by,
                 "LEGAL_OFFICER", {"feedback_id": feedback_id,
                                   "chunk_id": row["chunk_id"],
                                   "corpus_snapshot_id": resolved_snapshot_id})
    return {"feedback_id": feedback_id, "status": "RESOLVED",
            "corpus_snapshot_id": resolved_snapshot_id}


# ---------------------------------------------------------------- benchmark

def register_benchmark_cases(conn, cases, frozen_by):
    """Frozen, never-touched cases with confirmed-correct verdicts."""
    if not frozen_by:
        raise CoreError("frozen_by (a named human) is required")
    added = []
    for c in cases:
        for key in ("case_id", "slice_name", "input", "expected", "source_ref"):
            if key not in c:
                raise CoreError(f"benchmark case missing {key}")
        cid = uid()
        conn.execute(
            "INSERT INTO benchmark_case (id, case_id, slice_name, input_json, "
            "expected_json, source_ref, frozen_by, frozen_at) VALUES (?,?,?,?,?,?,?,?)",
            (cid, c["case_id"], c["slice_name"], _j(c["input"]), _j(c["expected"]),
             c["source_ref"], frozen_by, now()),
        )
        added.append(cid)
    append_event(conn, "SYSTEM", "BENCHMARK_CASES_FROZEN", frozen_by, "ML", {
        "count": len(added), "snapshot_hash": benchmark_snapshot_hash(conn),
    })
    return added


def benchmark_snapshot_hash(conn):
    rows = conn.execute(
        "SELECT case_id, slice_name, input_json, expected_json FROM benchmark_case "
        "ORDER BY case_id"
    ).fetchall()
    return sha256_text(canonical([list(r) for r in rows]))


# ---------------------------------------------------------------- training batch

def create_training_batch(conn, *, created_by, base_training_set,
                          min_corrections=MIN_CORRECTIONS_FOR_BATCH):
    if not created_by or not base_training_set:
        raise CoreError("created_by and base_training_set are required "
                        "(replaying the original set prevents catastrophic forgetting)")
    rows = conn.execute(
        "SELECT * FROM correction WHERE status='ACCEPTED' "
        "AND training_batch_id IS NULL ORDER BY submitted_at"
    ).fetchall()
    if len(rows) < min_corrections:
        raise CoreError(
            f"only {len(rows)} adjudicated corrections; {min_corrections} required "
            "before a retraining batch is meaningful"
        )
    examples = [{
        "field_path": r["field_path"], "ai_value": _parse(r["ai_value"]),
        "correct_value": _parse(r["correct_value"]), "reason": r["reason"],
        "adjudicated_by": r["adjudicated_by"],
    } for r in rows]
    snapshot_hash = sha256_text(canonical({
        "base_training_set": base_training_set, "examples": examples,
    }))
    bid = uid()
    conn.execute(
        "INSERT INTO training_batch (id, created_at, created_by, correction_count, "
        "base_training_set, training_snapshot_hash, status) VALUES (?,?,?,?,?,?,'CREATED')",
        (bid, now(), created_by, len(rows), base_training_set, snapshot_hash),
    )
    for r in rows:
        conn.execute("UPDATE correction SET training_batch_id=? WHERE id=?", (bid, r["id"]))
    append_event(conn, "SYSTEM", "TRAINING_BATCH_CREATED", created_by, "ML", {
        "batch_id": bid, "correction_count": len(rows),
        "training_snapshot_hash": snapshot_hash,
        "base_training_set": base_training_set,
    })
    return {"batch_id": bid, "correction_count": len(rows),
            "training_snapshot_hash": snapshot_hash, "examples": examples}


def register_candidate(conn, *, version, training_batch_id, q_lora_config,
                       corpus_snapshot_id=None, created_by="ml-engineer"):
    batch = _row(conn, "SELECT * FROM training_batch WHERE id=?", (training_batch_id,))
    if not batch:
        raise CoreError("unknown training batch")
    if not isinstance(q_lora_config, dict) or not q_lora_config.get("base_training_set"):
        raise CoreError("q_lora_config must include base_training_set (replay of original data)")
    if "replay_ratio" not in q_lora_config:
        raise CoreError("q_lora_config must include replay_ratio")
    vid = uid()
    conn.execute(
        "INSERT INTO model_version (id, version, model_type, training_batch_id, "
        "training_snapshot_hash, corpus_snapshot_id, q_lora_config_json, status, "
        "created_at) VALUES (?,?,'EXTRACTION',?,?,?,?,'CANDIDATE',?)",
        (vid, version, training_batch_id, batch["training_snapshot_hash"],
         corpus_snapshot_id, _j(q_lora_config), now()),
    )
    append_event(conn, "SYSTEM", "CANDIDATE_MODEL_REGISTERED", created_by, "ML", {
        "version": version, "training_batch_id": training_batch_id,
        "training_snapshot_hash": batch["training_snapshot_hash"],
        "corpus_snapshot_id": corpus_snapshot_id,
    })
    return {"version_id": vid, "version": version, "status": "CANDIDATE"}


# ---------------------------------------------------------------- evaluation

def current_production(conn):
    return _row(conn,
                "SELECT * FROM model_version WHERE status='PRODUCTION' "
                "ORDER BY deployed_at DESC LIMIT 1")


def _scores(results):
    per_slice = {}
    total_correct = total = 0
    for name, r in results.items():
        correct, count = int(r["correct"]), int(r["total"])
        if count <= 0 or correct > count or correct < 0:
            raise CoreError(f"invalid results for slice {name}")
        per_slice[name] = round(correct / count, 4)
        total_correct += correct
        total += count
    if total == 0:
        raise CoreError("empty benchmark results")
    return per_slice, round(total_correct / total, 4)


def evaluate_candidate(conn, version, results, evaluator):
    """Per-slice non-regression: a single aggregate score can hide a regression
    on a rare but critical document type."""
    candidate = _row(conn, "SELECT * FROM model_version WHERE version=?", (version,))
    if not candidate:
        raise CoreError("unknown candidate version")
    if candidate["status"] not in ("CANDIDATE", "APPROVED"):
        raise CoreError(f"candidate is already {candidate['status']}")
    if not evaluator:
        raise CoreError("evaluator (a named human or CI identity) is required")
    if not results:
        raise CoreError("results are required")
    per_slice, overall = _scores(results)
    production = current_production(conn)
    prod_slices = _parse(production["per_slice_json"], {}) if production else {}
    prod_score = production["eval_score"] if production else None
    regressions = []
    if production:
        for name, score in prod_slices.items():
            got = per_slice.get(name)
            if got is None:
                regressions.append({"slice": name, "problem": "slice missing from evaluation",
                                    "production": score, "candidate": None})
            elif got < score - REGRESSION_TOLERANCE:
                regressions.append({"slice": name, "production": score,
                                    "candidate": got, "delta": round(got - score, 4)})
        if prod_score is not None and overall < prod_score - REGRESSION_TOLERANCE:
            regressions.append({"slice": "__overall__", "production": prod_score,
                                "candidate": overall,
                                "delta": round(overall - prod_score, 4)})
        passed = not regressions
    else:
        passed = (overall >= MIN_ABSOLUTE_SCORE
                  and all(s >= MIN_ABSOLUTE_SLICE_SCORE for s in per_slice.values()))
        if not passed:
            regressions.append({"slice": "__absolute_threshold__",
                                "required": MIN_ABSOLUTE_SCORE, "candidate": overall})

    run_id = uid()
    conn.execute(
        "INSERT INTO eval_run (id, candidate_version_id, benchmark_snapshot_hash, "
        "per_slice_json, overall_score, production_score, production_slice_json, "
        "regressions_json, passed, evaluator, run_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, candidate["id"], benchmark_snapshot_hash(conn), _j(per_slice),
         overall, prod_score, _j(prod_slices) if production else None,
         _j(regressions), int(passed), evaluator, now()),
    )
    conn.execute(
        "UPDATE model_version SET eval_run_id=?, eval_score=?, per_slice_json=? "
        "WHERE id=?",
        (run_id, overall, _j(per_slice), candidate["id"]),
    )
    append_event(conn, "SYSTEM", "CANDIDATE_EVALUATED", evaluator, "ML", {
        "version": version, "overall_score": overall,
        "per_slice": per_slice, "regressions": regressions,
        "passed": passed, "benchmark_snapshot_hash": benchmark_snapshot_hash(conn),
    })
    return {"eval_run_id": run_id, "passed": passed, "overall_score": overall,
            "per_slice": per_slice, "regressions": regressions,
            "production_score": prod_score}


# ---------------------------------------------------------------- human gates

def approve_candidate(conn, version, approver, note=""):
    """HUMAN REVIEW GATE — required, not optional."""
    if not approver:
        raise CoreError("approver (a named human) is required")
    candidate = _row(conn, "SELECT * FROM model_version WHERE version=?", (version,))
    if not candidate:
        raise CoreError("unknown candidate version")
    run = _row(conn, "SELECT * FROM eval_run WHERE id=?", (candidate["eval_run_id"],))
    if not run or not run["passed"]:
        raise CoreError("candidate has no passing evaluation; human gate cannot be bypassed")
    if candidate["status"] != "CANDIDATE":
        raise CoreError(f"candidate is already {candidate['status']}")
    conn.execute(
        "UPDATE model_version SET status='APPROVED', approver=?, approved_at=?, "
        "approval_note=? WHERE id=?",
        (approver, now(), note, candidate["id"]),
    )
    append_event(conn, "SYSTEM", "MODEL_APPROVED", approver, "HUMAN_REVIEW_GATE", {
        "version": version, "eval_run_id": run["id"],
        "eval_score": run["overall_score"], "note": note,
    })
    return {"version": version, "status": "APPROVED", "approver": approver}


def deploy_candidate(conn, version, deployed_by, strategy="canary"):
    if strategy not in VALID_DEPLOY_STRATEGIES:
        raise CoreError(f"strategy must be one of {VALID_DEPLOY_STRATEGIES}")
    if not deployed_by:
        raise CoreError("deployed_by is required")
    candidate = _row(conn, "SELECT * FROM model_version WHERE version=?", (version,))
    if not candidate or candidate["status"] != "APPROVED":
        raise CoreError("only an APPROVED candidate can be deployed")
    production = current_production(conn)
    if strategy == "direct" and production is not None:
        raise CoreError(
            "direct deployment is only allowed to bootstrap the FIRST production "
            "model; use strategy='canary' then promote_canary() so a different "
            "human signs off before replacing a live model"
        )
    status = "CANARY" if strategy == "canary" else "PRODUCTION"
    old = production if strategy == "direct" else None
    if old:
        conn.execute("UPDATE model_version SET status='ROLLED_BACK' WHERE id=?", (old["id"],))
    conn.execute(
        "UPDATE model_version SET status=?, deployed_at=?, deployed_by=?, "
        "rollback_target=? WHERE id=?",
        (status, now(), deployed_by, old["id"] if old else None, candidate["id"]),
    )
    append_event(conn, "SYSTEM", "MODEL_DEPLOYED", deployed_by, "ML", {
        "version": version, "strategy": strategy, "status": status,
        "previous_production": old["version"] if old else None,
    })
    return {"version": version, "status": status, "strategy": strategy}


def promote_canary(conn, version, approver):
    """Second human sign-off before a canary becomes production."""
    if not approver:
        raise CoreError("approver is required")
    candidate = _row(conn, "SELECT * FROM model_version WHERE version=?", (version,))
    if not candidate or candidate["status"] != "CANARY":
        raise CoreError("only a CANARY version can be promoted")
    if approver == candidate["deployed_by"]:
        raise CoreError("a different human must approve production promotion")
    old = current_production(conn)
    if old:
        conn.execute("UPDATE model_version SET status='ROLLED_BACK' WHERE id=?", (old["id"],))
    conn.execute(
        "UPDATE model_version SET status='PRODUCTION', deployed_at=?, "
        "rollback_target=? WHERE id=?",
        (now(), old["id"] if old else None, candidate["id"]),
    )
    append_event(conn, "SYSTEM", "MODEL_PROMOTED", approver, "HUMAN_REVIEW_GATE", {
        "version": version, "previous_production": old["version"] if old else None,
        "rollback_target": old["version"] if old else None,
    })
    return {"version": version, "status": "PRODUCTION",
            "previous_production": old["version"] if old else None}


def rollback(conn, to_version, operator, reason):
    if not operator or not reason:
        raise CoreError("operator and reason are required for a rollback")
    target = _row(conn, "SELECT * FROM model_version WHERE version=?", (to_version,))
    if not target:
        raise CoreError("unknown rollback target version")
    if target["status"] not in ("ROLLED_BACK", "APPROVED"):
        raise CoreError(f"rollback target has status {target['status']}")
    current = current_production(conn)
    if current:
        conn.execute("UPDATE model_version SET status='ROLLED_BACK' WHERE id=?",
                     (current["id"],))
    conn.execute(
        "UPDATE model_version SET status='PRODUCTION', deployed_at=? WHERE id=?",
        (now(), target["id"]),
    )
    append_event(conn, "SYSTEM", "MODEL_ROLLED_BACK", operator, "HUMAN_REVIEW_GATE", {
        "to_version": to_version, "from_version": current["version"] if current else None,
        "reason": reason,
    })
    return {"production": to_version, "rolled_back_from":
            current["version"] if current else None}


# ---------------------------------------------------------------- reporting

def verdict_provenance(conn, tx_id):
    rows = conn.execute(
        "SELECT payload, created_at FROM verdict WHERE tx_id=? ORDER BY created_at", (tx_id,)
    ).fetchall()
    out = []
    for r in rows:
        v = _parse(r["payload"], {})
        out.append({
            "created_at": r["created_at"],
            "extraction_model_version": v.get("model_version"),
            "rules_engine_version": v.get("rules_engine_version"),
            "ruleset_version": v.get("ruleset_version"),
            "corpus_snapshot_id": v.get("corpus_snapshot_id"),
            "prompt_hash": v.get("prompt_hash"),
        })
    prod = current_production(conn)
    return {"tx_id": tx_id, "verdicts": out,
            "current_production_model": prod["version"] if prod else None}


def learning_dashboard(conn):
    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()["c"]
    return {
        "pending_corrections": count("SELECT count(*) c FROM correction WHERE status='PENDING'"),
        "accepted_corrections": count("SELECT count(*) c FROM correction WHERE status='ACCEPTED'"),
        "rejected_corrections": count("SELECT count(*) c FROM correction WHERE status='REJECTED'"),
        "open_citation_feedback": count("SELECT count(*) c FROM citation_feedback WHERE status='OPEN'"),
        "benchmark_cases": count("SELECT count(*) c FROM benchmark_case"),
        "benchmark_slices": [r["slice_name"] for r in conn.execute(
            "SELECT DISTINCT slice_name FROM benchmark_case ORDER BY slice_name")],
        "benchmark_snapshot_hash": benchmark_snapshot_hash(conn),
        "training_batches": count("SELECT count(*) c FROM training_batch"),
        "candidate_models": count("SELECT count(*) c FROM model_version WHERE status='CANDIDATE'"),
        "approved_models": count("SELECT count(*) c FROM model_version WHERE status='APPROVED'"),
        "eval_runs": count("SELECT count(*) c FROM eval_run"),
        "current_production": (current_production(conn) or {}).get("version"),
        "human_gate_required": True,
    }
