"""Track A scaffolding: training data format + loopback extraction server contract.

This module helps integrate a real Qwen2.5-3B fine-tune with ProcEMP.
It is stdlib-only and prototype-oriented.

Contract expected by ``extraction.LocalHTTPBackend`` (POST JSON to loopback URL):
Request:
  {
    "model_version": "...",
    "schema": {...},               # seed_extraction_schema.json
    "pages": [{"page": 1, "text": "..."}],
    "options": {"temperature": 0, "strict_json_schema": true}
  }
Response (JSON object):
  {
    "model_version": "...",
    "documents": {
      "<document_type>": {
        "status": "OK" | "UNREADABLE",
        "fields": {...},            # typed values matching schema
        "confidence": {...},        # 0..1 per field
        "evidence": {...}           # optional {page,bbox,snippet}
      }
    }
  }

ProcEMP validates responses strictly (``extraction.validate_output``). Unknown
document keys/hallucinated fields, wrong types, bad confidences, or malformed
JSON become ABSTAIN/failed validation — never silent acceptance.

Training data produced here should be reviewed for RA 10173/NPC/privacy and
procurement confidentiality before use (see procurement-review/05-data-governance-training.md).
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import canonical, now, sha256_text
from extraction import SCHEMA_PATH, load_schema, valid_stub_response, validate_output


def _pages_text(pages):
    parts = []
    for p in pages or []:
        if isinstance(p, dict):
            parts.append(f"[page {p.get('page', '?')}]\n{p.get('text', '')}")
        else:
            parts.append(str(p))
    return "\n\n".join(parts).strip()


def build_instruction(schema, pages):
    schema_version = schema.get("schema_version", "unknown")
    doc_types = schema.get("document_types", {})
    return (
        "You are a procurement document field extractor. "
        "Return ONLY a JSON object that matches the required schema. "
        "Do not make legal/procurement decisions, do not disqualify bidders, "
        "and do not invent fields. Use only known document types and known fields. "
        "If a document cannot be read, set status to UNREADABLE. "
        "Include per-field confidence values from 0 to 1 and evidence when available.\n\n"
        f"Schema version: {schema_version}\n"
        f"Allowed document types and field types: {canonical(doc_types)}\n"
    )


def build_sft_record(*, pages, target_response, schema=None, model_version=None,
                     metadata=None):
    """Build a JSONL-ready SFT record for Qwen-style chat fine-tuning.

    The default shape uses ``messages`` (system/user/assistant), which is widely
    supported. ``metadata`` is kept for provenance and can be stripped before
    training if your framework does not accept extra keys.
    """
    schema = schema or load_schema()
    target = json.loads(canonical(target_response))
    if model_version and not target.get("model_version"):
        target["model_version"] = model_version
    validation = validate_output(target, schema, pages)
    record = {
        "messages": [
            {"role": "system", "content": build_instruction(schema, pages)},
            {"role": "user", "content": _pages_text(pages) or "(no text provided)"},
            {"role": "assistant", "content": canonical(target)},
        ],
        "metadata": {
            "schema_version": schema.get("schema_version"),
            "page_count": len(pages or []),
            "prompt_hash": sha256_text(canonical({"schema": schema, "pages": pages})),
            "target_valid": validation.valid,
            "target_violations": validation.violations,
            "created_at": now(),
            **(metadata or {}),
        },
    }
    return record


def build_completion_record(*, pages, target_response, schema=None, metadata=None):
    """Alternative prompt/completion shape for frameworks that prefer it."""
    schema = schema or load_schema()
    target = json.loads(canonical(target_response))
    validation = validate_output(target, schema, pages)
    prompt = build_instruction(schema, pages) + "\nDocument text:\n" + (_pages_text(pages) or "(none)")
    return {
        "prompt": prompt,
        "completion": canonical(target),
        "metadata": {
            "schema_version": schema.get("schema_version"),
            "target_valid": validation.valid,
            "target_violations": validation.violations,
            "created_at": now(),
            **(metadata or {}),
        },
    }


def write_jsonl(records, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(canonical(record) + "\n")
    return path


class _StubHandler(BaseHTTPRequestHandler):
    responder = None
    expected_schema = None

    def log_message(self, format, *args):  # noqa: A003
        return

    def _send(self, code, payload):
        body = canonical(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send(400, {"error": f"invalid JSON request: {exc}"})
            return
        responder = self.responder or (lambda request: valid_stub_response(
            request.get("model_version", "qwen2.5-3b-ft-procurement-stub")))
        try:
            response = responder(req)
            if not isinstance(response, dict):
                raise ValueError("responder must return a JSON object")
            self._send(200, response)
        except Exception as exc:
            self._send(500, {"error": str(exc)})


def serve_stub(*, host="127.0.0.1", port=8080, responder=None):
    """Run a loopback extraction stub compatible with LocalHTTPBackend.

    For production, replace with a real local model server (vLLM/llama.cpp/Ollama)
    that returns schema-valid extraction JSON and keeps data on loopback.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("serve_stub refuses non-loopback hosts; keep extraction local")
    handler = type("Handler", (_StubHandler,), {"responder": staticmethod(responder) if responder else None})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"ProcEMP extraction stub listening on http://{host}:{port}/ (loopback only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="ProcEMP training/extraction adapter scaffolding")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("jsonl-stub", help="Write a stub SFT JSONL record from valid_stub_response()")
    p.add_argument("--out", required=True)
    p.add_argument("--schema", default=SCHEMA_PATH)
    p.add_argument("--format", choices=["messages", "completion"], default="messages")
    p.add_argument("--model-version", default="qwen2.5-3b-ft-procurement-stub")

    p = sub.add_parser("serve-stub", help="Run a loopback HTTP stub for LocalHTTPBackend integration tests")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)
    schema = load_schema(args.schema) if getattr(args, "schema", None) else load_schema()
    if args.cmd == "jsonl-stub":
        pages = [{"page": 1, "text": "PhilGEPS Platinum, valid until 2027-06-30"},
                 {"page": 2, "text": "Financial proposal: PHP 950,000.00"}]
        target = valid_stub_response(args.model_version)
        record = (build_sft_record(pages=pages, target_response=target, schema=schema,
                                   model_version=args.model_version,
                                   metadata={"source": "valid_stub_response"})
                  if args.format == "messages"
                  else build_completion_record(pages=pages, target_response=target,
                                               schema=schema, metadata={"source": "valid_stub_response"}))
        write_jsonl([record], args.out)
        print(canonical({"wrote": args.out, "target_valid": record["metadata"]["target_valid"],
                         "format": args.format}))
        return 0
    if args.cmd == "serve-stub":
        serve_stub(host=args.host, port=args.port)
        return 0
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())