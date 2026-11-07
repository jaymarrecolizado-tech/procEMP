"""Schema-constrained adapter for real local LLMs (no training/fine-tune required).

Sits between ProcEMP's ``extraction.LocalHTTPBackend`` contract and a local
chat/completions model server (Ollama, llama.cpp OpenAI-compatible server,
vLLM, etc.). It prompts the model to return ProcEMP extraction JSON, extracts
JSON from the response, performs light shape normalization, and returns a JSON
object for ProcEMP validation.

IMPORTANT:
- Advisory plumbing/integration only. It does NOT make procurement decisions.
- ProcEMP still fail-closes: invalid schema/confidence/types/hallucinated
  fields become ABSTAIN through ``extraction.validate_output``/checker.
- Use approved, non-confidential samples only. Keep upstream and this adapter
  on loopback. Verify RA 10173/NPC/DPIA/governance before production use.
- A base (non-fine-tuned) model may have low schema-valid rates; that is why
  validation and human review exist.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import CoreError, canonical
from extraction import load_schema, validate_output
from training_adapter import _pages_text, build_instruction


def _assert_loopback(url, label="endpoint"):
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    try:
        import ipaddress
        if ipaddress.ip_address(host).is_loopback:
            return
    except Exception:
        pass
    raise CoreError(f"refusing non-loopback {label}: {host}")


def _extract_json(text):
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    # fenced block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # first object .. last object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


def normalize_response(obj, *, model_version):
    """Best-effort normalization for common model output mistakes.

    Never invents document/field values. Only adjusts envelope shape and fills
    missing model_version/confidence maps where safely inferable.
    """
    if not isinstance(obj, dict):
        return obj
    out = json.loads(canonical(obj))
    out.setdefault("model_version", model_version)
    docs = out.get("documents")
    if not isinstance(docs, dict):
        # sometimes model returns documents as list or puts fields at top level
        if isinstance(docs, list):
            mapped = {}
            for item in docs:
                if isinstance(item, dict) and item.get("type"):
                    mapped[item["type"]] = {k: v for k, v in item.items() if k != "type"}
            if mapped:
                out["documents"] = mapped
                docs = mapped
        else:
            return out
    for key, doc in list(docs.items()):
        if not isinstance(doc, dict):
            continue
        doc.setdefault("status", "OK")
        if "fields" in doc and "confidence" not in doc and isinstance(doc["fields"], dict):
            # unknown confidence -> 0 so ProcEMP abstains rather than over-trusting
            doc["confidence"] = {k: 0.0 for k in doc["fields"].keys()}
        if "confidence" in doc and "fields" not in doc:
            # invalid shape; leave for validator
            pass
    return out


class UpstreamClient:
    def __init__(self, kind, url, model, timeout=120):
        self.kind = kind
        self.url = url
        self.model = model
        self.timeout = timeout
        _assert_loopback(url, "upstream")

    def complete(self, system, user):
        if self.kind == "ollama":
            payload = {
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {"temperature": 0},
            }
            data = self._post(self.url, payload)
            return ((data.get("message") or {}).get("content") or "")
        if self.kind == "openai":
            payload = {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            data = self._post(self.url, payload)
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            return msg.get("content") or choices[0].get("text") or ""
        if self.kind == "llama_cpp":
            payload = {
                "prompt": system + "\n\n" + user,
                "temperature": 0,
                "stream": False,
            }
            data = self._post(self.url, payload)
            return data.get("content") or data.get("generation") or data.get("text") or ""
        raise CoreError(f"unsupported upstream kind: {self.kind}")

    def _post(self, url, payload):
        req = urllib.request.Request(
            url,
            data=canonical(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except Exception as exc:
            raise CoreError(f"upstream request failed: {exc}") from exc
        try:
            return json.loads(body)
        except Exception as exc:
            raise CoreError(f"upstream returned non-JSON: {exc}; body={body[:300]}") from exc


class Adapter:
    def __init__(self, upstream, schema=None):
        self.upstream = upstream
        self.schema = schema or load_schema()

    def handle(self, request):
        pages = request.get("pages") or []
        schema = request.get("schema") or self.schema
        model_version = request.get("model_version") or self.upstream.model
        system = build_instruction(schema, pages)
        system += (
            "\nRespond with ONLY a single JSON object. No markdown, no explanation. "
            "The top-level keys must include model_version and documents."
        )
        user = _pages_text(pages) or "(no text provided)"
        text = self.upstream.complete(system, user)
        obj = _extract_json(text)
        if obj is None:
            return {
                "model_version": model_version,
                "documents": {},
                "_adapter_error": "model did not return parseable JSON",
                "_adapter_excerpt": (text or "")[:500],
            }
        obj = normalize_response(obj, model_version=model_version)
        validation = validate_output(obj, schema, pages)
        if not validation.valid:
            obj["_adapter_validation"] = {
                "valid": False,
                "violations": validation.violations,
                "uncertain_fields": validation.uncertain_fields,
                "note": "ProcEMP will fail closed (ABSTAIN) on invalid extraction output.",
            }
        return obj


def make_handler(adapter):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("[adapter] " + (fmt % args) + "\n")

        def do_POST(self):  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                req = json.loads(raw.decode("utf-8"))
                resp = adapter.handle(req)
                body = canonical(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = canonical({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            body = canonical({
                "status": "ok",
                "service": "procemp-extraction-adapter",
                "advisory_only": True,
                "contract": "POST JSON {model_version,schema,pages,options}",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(host="127.0.0.1", port=8092, *, upstream_kind, upstream_url, model, timeout=120):
    _assert_loopback(f"http://{host}", "bind host")
    upstream = UpstreamClient(upstream_kind, upstream_url, model, timeout=timeout)
    adapter = Adapter(upstream)
    server = ThreadingHTTPServer((host, port), make_handler(adapter))
    print(f"ProcEMP extraction adapter on http://{host}:{port}/ (loopback)")
    print(f"Upstream: {upstream_kind} {upstream_url} model={model}")
    print("Advisory integration only; ProcEMP validates/fail-closes downstream.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Loopback ProcEMP extraction adapter for local LLM servers")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8092)
    p.add_argument("--upstream-kind", required=True, choices=["ollama", "openai", "llama_cpp"])
    p.add_argument("--upstream-url", required=True,
                   help="Ollama: http://127.0.0.1:11434/api/chat; OpenAI-compatible: http://127.0.0.1:8080/v1/chat/completions; llama.cpp: http://127.0.0.1:8080/completion")
    p.add_argument("--model", required=True, help="Upstream model name/id")
    p.add_argument("--timeout", type=int, default=120)
    args = p.parse_args(argv)
    serve(args.host, args.port, upstream_kind=args.upstream_kind,
          upstream_url=args.upstream_url, model=args.model, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())