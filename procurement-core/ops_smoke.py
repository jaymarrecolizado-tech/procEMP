"""Read-only deployment smoke check — the code half of a runbook rehearsal.

Runs the checks an operator would otherwise do by hand before/after a
deployment: ledger hash chain, corpus passage hashes, seed/registry readiness,
and (optionally) live loopback service health endpoints. It never writes to the
database and never makes procurement decisions.

Exit code is 0 only when every *error-severity* check passes. Readiness
blockers are reported as advisory info (the prototype legitimately has
fail-closed blockers), so a clean run does not mean "ready for production".

  python ops_smoke.py --db procurement.db
  python ops_smoke.py --db procurement.db \
      --review-url http://127.0.0.1:8091/health \
      --coa-url http://127.0.0.1:8090/health
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

from core import _connect, now, verify_chain
from model_plumbing_check import _assert_loopback_url
from rag import verify_corpus_integrity
from readiness_report import build_report

DEFAULT_TIMEOUT = 3


def _check(name, ok, detail, severity="error"):
    return {"name": name, "ok": bool(ok), "severity": severity, "detail": detail}


def _health(url, timeout=DEFAULT_TIMEOUT):
    _assert_loopback_url(url, "health url")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return resp.status, payload


def run_smoke(db_path, review_url=None, coa_url=None, timeout=DEFAULT_TIMEOUT):
    """Run read-only checks against a database file (and optional live services)."""
    if not os.path.exists(db_path):
        result = {
            "generated_at": now(),
            "advisory_only": True,
            "decision_making": False,
            "db": db_path,
            "ok": False,
            "failed_checks": ["database"],
            "checks": [_check("database", False,
                              f"database file not found: {db_path}")],
        }
        return result
    checks = []
    conn = _connect(db_path)
    try:
        try:
            chain = verify_chain(conn)
            checks.append(_check("ledger_chain", chain.get("ok", False), chain))
        except Exception as exc:  # pragma: no cover - defensive reporting
            checks.append(_check("ledger_chain", False, str(exc)))
        try:
            corpus = verify_corpus_integrity(conn)
            checks.append(_check("corpus_integrity", corpus.get("ok", False), corpus))
        except Exception as exc:  # pragma: no cover - defensive reporting
            checks.append(_check("corpus_integrity", False, str(exc)))
    finally:
        conn.close()

    try:
        report = build_report(db_path)
        checks.append(_check(
            "readiness", True,
            {"ready_for_production": report["ready_for_production"],
             "blockers": report["blockers"]},
            severity="info",
        ))
    except Exception as exc:  # pragma: no cover - defensive reporting
        checks.append(_check("readiness", False, str(exc)))

    for label, url in (("review_ui_health", review_url), ("coa_health", coa_url)):
        if not url:
            continue
        try:
            status, payload = _health(url, timeout=timeout)
            checks.append(_check(
                label, status == 200 and payload.get("status") == "ok",
                {"status": status, "payload": payload},
            ))
        except Exception as exc:
            checks.append(_check(label, False, str(exc)))

    failures = [c for c in checks if c["severity"] == "error" and not c["ok"]]
    return {
        "generated_at": now(),
        "advisory_only": True,
        "decision_making": False,
        "db": db_path,
        "ok": not failures,
        "failed_checks": [c["name"] for c in failures],
        "checks": checks,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only deployment smoke check (no writes, no decisions)."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--review-url", default=None,
                        help="e.g. http://127.0.0.1:8091/health (loopback only)")
    parser.add_argument("--coa-url", default=None,
                        help="e.g. http://127.0.0.1:8090/health (loopback only)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    result = run_smoke(args.db, review_url=args.review_url,
                       coa_url=args.coa_url, timeout=args.timeout)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())