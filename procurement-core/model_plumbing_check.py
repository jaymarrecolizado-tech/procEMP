"""Validate real local-model plumbing before QLoRA fine-tuning.

This checks that a loopback extraction endpoint can integrate with
``extraction.LocalHTTPBackend`` and return schema-valid JSON for ProcEMP.
It does NOT train a model and does NOT make procurement decisions.

Use with an approved, non-confidential sample set. Keep data local/loopback.

Examples:
  python model_plumbing_check.py stub --port 8080
  python model_plumbing_check.py check --url http://127.0.0.1:8080/extract
  python model_plumbing_check.py check --url http://127.0.0.1:11434/api/generate  # not directly compatible
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from http.server import ThreadingHTTPServer

from core import canonical, now
from extraction import ExtractionError, LocalHTTPBackend, load_schema, valid_stub_response, validate_output
from training_adapter import _StubHandler, build_instruction


SAMPLE_CASES = [
    {
        "name": "clean_digital_text",
        "pages": [
            {"page": 1, "text": "PhilGEPS Platinum Registration, valid until 2027-06-30"},
            {"page": 2, "text": "Financial proposal: PHP 950,000.00"},
            {"page": 4, "text": "Omnibus Sworn Statement signed and notarized. Bid Securing Declaration signed."},
        ],
    },
    {
        "name": "sparse_text",
        "pages": [{"page": 1, "text": "Registration attached."}],
    },
]


def _start_stub(port=0):
    handler = type("Handler", (_StubHandler,), {"responder": None})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    bound = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{bound}/extract"


def check_endpoint(url, *, schema=None, cases=None, model_version="qwen2.5-3b-procurement-check", timeout=30):
    schema = schema or load_schema()
    cases = cases or SAMPLE_CASES
    backend = LocalHTTPBackend(url=url, model_version=model_version, timeout=timeout)
    results = []
    latencies = []
    for case in cases:
        t0 = time.time()
        row = {
            "case": case["name"],
            "page_count": len(case["pages"]),
            "ok": False,
            "schema_valid": False,
            "violations": [],
            "uncertain_fields": [],
            "document_count": 0,
            "has_model_version": False,
            "latency_ms": None,
            "error": None,
        }
        try:
            raw = backend.extract(case["pages"], schema)
            row["latency_ms"] = round((time.time() - t0) * 1000, 1)
            latencies.append(row["latency_ms"])
            validation = validate_output(raw, schema, case["pages"])
            row["ok"] = True
            row["schema_valid"] = validation.valid
            row["violations"] = validation.violations
            row["uncertain_fields"] = validation.uncertain_fields
            row["document_count"] = len(validation.documents)
            row["has_model_version"] = bool(validation.model_version)
        except (ExtractionError, Exception) as exc:
            row["latency_ms"] = round((time.time() - t0) * 1000, 1)
            row["error"] = str(exc)
        results.append(row)

    schema_valid = sum(1 for r in results if r["schema_valid"])
    transport_ok = sum(1 for r in results if r["ok"])
    summary = {
        "checked_at": now(),
        "url": url,
        "model_version": model_version,
        "cases": len(results),
        "transport_ok": transport_ok,
        "schema_valid": schema_valid,
        "schema_valid_rate": round(schema_valid / len(results), 4) if results else 0.0,
        "avg_latency_ms": round(statistics.mean(latencies), 1) if latencies else None,
        "advisory_only": True,
        "decision_making": False,
        "notes": [
            "A non-fine-tuned base model may return invalid schema; ProcEMP fail-closes to ABSTAIN on invalid output.",
            "Use only approved, non-confidential samples for plumbing checks.",
            "Endpoint must be loopback; LocalHTTPBackend refuses non-loopback URLs.",
        ],
        "results": results,
    }
    return summary


def prompt_hint(schema=None):
    schema = schema or load_schema()
    return {
        "instruction": build_instruction(schema, [{"page": 1, "text": "<document text>"}]),
        "required_response_shape": {
            "model_version": "string",
            "documents": {
                "<allowed document type>": {
                    "status": "OK|UNREADABLE",
                    "fields": {"<allowed field>": "<typed value>"},
                    "confidence": {"<allowed field>": 0.0},
                    "evidence": {"<allowed field>": {"page": 1, "snippet": "..."}},
                }
            },
        },
        "contract": "POST JSON {model_version,schema,pages,options} -> JSON extraction object",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check local extraction model plumbing (loopback only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stub", help="Run a contract-compatible loopback stub server")
    p.add_argument("--port", type=int, default=8080)

    p = sub.add_parser("check", help="Check an extraction endpoint against ProcEMP schema")
    p.add_argument("--url", required=True)
    p.add_argument("--model-version", default="qwen2.5-3b-procurement-check")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("prompt-hint", help="Print instruction/response shape hints")

    args = parser.parse_args(argv)
    if args.cmd == "stub":
        server, url = _start_stub(args.port)
        print(f"Stub extraction endpoint: {url}")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return 0
    if args.cmd == "check":
        summary = check_endpoint(args.url, model_version=args.model_version, timeout=args.timeout)
        if args.json:
            print(canonical(summary))
        else:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["transport_ok"] == summary["cases"] else 1
    if args.cmd == "prompt-hint":
        print(json.dumps(prompt_hint(), indent=2, ensure_ascii=False))
        return 0
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())