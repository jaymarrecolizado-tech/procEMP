"""Step 6: OCR / extraction adapter — where the fine-tuned Qwen2.5-3B plugs in.

The model is a *field extractor*, never a decision maker. It returns typed fields,
per-field confidence and evidence spans. This module validates its output strictly
and feeds only clean fields into the deterministic checker (checker.py).

Fail-closed behaviour:
  - extraction before public bid opening        -> PolicyBlocked
  - model unreachable / malformed response     -> ExtractionError + event, no verdict
  - schema violation / hallucinated fields    -> ABSTAIN verdict, never FAIL
  - per-field confidence below threshold      -> field omitted -> checker abstains
  - unreadable document                      -> ABSTAIN, never a disqualification

No external AI API: LocalHTTPBackend only accepts loopback URLs. There are no
model weights in this repo; StubBackend is used for tests and the demo.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from core import (
    CoreError, PolicyBlocked, append_event, canonical, get_tx, has_event,
    now, sha256_text, submit_verdict, _connect,
)
from checker import evaluate_submission

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "seed_extraction_schema.json")


class ExtractionError(CoreError):
    pass


# ---------------------------------------------------------------- schema

def load_schema(path=SCHEMA_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- OCR

class OCRBackend:
    def pages(self, pages):
        raise NotImplementedError


class PassthroughOCR(OCRBackend):
    """Digital PDFs / already-extracted text. Adds page numbers if absent."""
    def pages(self, pages):
        out = []
        for i, page in enumerate(pages or [], start=1):
            if isinstance(page, str):
                out.append({"page": i, "text": page})
            elif isinstance(page, dict):
                out.append({"page": page.get("page", i), "text": page.get("text", "")})
            else:
                raise ExtractionError(f"unsupported page object: {type(page)}")
        return out


# ---------------------------------------------------------------- PDF render

_PAGE_RE = re.compile(r"-(\d+)\.[^.]+$")


def _page_number(path):
    match = _PAGE_RE.search(os.path.basename(path))
    return int(match.group(1)) if match else 0


def _as_command_list(value):
    if isinstance(value, str):
        return [value]
    return list(value)


def render_pdf_to_images(pdf_path, output_dir=None, *, dpi=300, prefix="page",
                         fmt="png", poppler=("pdftoppm",), timeout=180):
    """Render a local PDF to page images using Poppler's ``pdftoppm``.

    No network. Requires the operator-installed Poppler binaries; a missing
    renderer fails closed with a clear error (never a silent partial render).
    Returns the image paths ordered by page number.
    """
    if not os.path.isfile(pdf_path):
        raise ExtractionError(f"PDF not found: {pdf_path}")
    try:
        dpi = int(dpi)
    except (TypeError, ValueError) as exc:
        raise ExtractionError(f"dpi must be an integer, got {dpi!r}") from exc
    if not 72 <= dpi <= 600:
        raise ExtractionError("dpi must be between 72 and 600")
    fmt = str(fmt).lower()
    if fmt not in ("png", "jpg", "jpeg", "ppm", "pgm", "tiff"):
        raise ExtractionError(f"unsupported image format: {fmt}")

    output_dir = output_dir or tempfile.mkdtemp(prefix="procemp-pdf-")
    os.makedirs(output_dir, exist_ok=True)
    out_prefix = os.path.join(output_dir, prefix)
    poppler = _as_command_list(poppler)
    cmd = poppler + ["-r", str(dpi), "-" + fmt, pdf_path, out_prefix]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                      check=True)
    except FileNotFoundError as exc:
        raise ExtractionError(
            f"local renderer {poppler[0]!r} is not installed. Install Poppler "
            "(pdftoppm) on the workstation; see procurement-review/06-deployment-runbook.md"
        ) from exc
    except subprocess.SubprocessError as exc:
        error = getattr(exc, "stderr", None) or str(exc)
        raise ExtractionError(f"PDF render failed: {error}") from exc
    images = [
        os.path.join(output_dir, name)
        for name in os.listdir(output_dir)
        if name.startswith(prefix + "-") and name.lower().endswith("." + fmt)
    ]
    images.sort(key=_page_number)
    if not images:
        raise ExtractionError("renderer produced no page images for the PDF")
    return images


_LANG_RE = re.compile(r"^[a-z]{2,3}(\+[a-z]{2,3})*$", re.IGNORECASE)


class TesseractOCR(OCRBackend):
    """Optional local OCR. Requires a local `tesseract` binary; no network.

    ``language`` follows tesseract's ``-l`` spec (``eng`` default, ``fil``
    optional, ``eng+fil`` for mixed Taglish scans). Anything else is rejected.
    """

    def __init__(self, language="eng", tesseract=("tesseract",)):
        if not _LANG_RE.match(str(language or "")):
            raise ExtractionError(
                f"invalid tesseract language spec: {language!r} "
                "(examples: 'eng', 'fil', 'eng+fil')"
            )
        self.language = language
        self.tesseract = _as_command_list(tesseract)

    def pages(self, pages):
        out = []
        for i, page in enumerate(pages or [], start=1):
            path = page.get("image_path") if isinstance(page, dict) else None
            if not path or not os.path.exists(path):
                raise ExtractionError(f"image not found for page {i}: {path}")
            try:
                result = subprocess.run(
                    self.tesseract + [path, "stdout", "-l", self.language],
                    capture_output=True, text=True, timeout=60, check=True,
                )
            except FileNotFoundError as exc:
                raise ExtractionError(
                    f"local {self.tesseract[0]!r} binary not installed; see "
                    "procurement-review/06-deployment-runbook.md"
                ) from exc
            except subprocess.SubprocessError as exc:
                error = getattr(exc, "stderr", None) or str(exc)
                raise ExtractionError(f"OCR failed on page {i}: {error}") from exc
            out.append({"page": i, "text": result.stdout, "ocr": "tesseract"})
        return out


# ---------------------------------------------------------------- backends

class ExtractionBackend:
    def extract(self, pages, schema):
        raise NotImplementedError


class StubBackend(ExtractionBackend):
    """Deterministic stand-in for the fine-tuned model (tests, demos, dry runs)."""
    def __init__(self, response):
        self.response = response

    def extract(self, pages, schema):
        if callable(self.response):
            return self.response(pages, schema)
        return json.loads(json.dumps(self.response))


class LocalHTTPBackend(ExtractionBackend):
    """Calls a locally-hosted inference server. Loopback only — never an
    external or cloud AI API. Point this at your Qwen2.5-3B fine-tune served by
    llama.cpp / vLLM / Ollama on the workstation or the VPS."""
    def __init__(self, url="http://127.0.0.1:8080/extract", model_version=None,
                 timeout=30):
        self.url = url
        self.model_version = model_version or "qwen2.5-3b-ft-procurement-v1"
        self.timeout = timeout
        self._assert_loopback(url)

    @staticmethod
    def _assert_loopback(url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise CoreError("only http(s) loopback endpoints are allowed")
        host = parsed.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise CoreError(
            f"external AI endpoint refused: {host}. Bid data must never leave "
            "the agency machine; use a local model server on loopback."
        )

    def extract(self, pages, schema):
        payload = {
            "model_version": self.model_version,
            "schema": schema,
            "pages": pages,
            "options": {"temperature": 0, "strict_json_schema": True},
        }
        req = urllib.request.Request(
            self.url, data=canonical(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except Exception as exc:
            raise ExtractionError(f"local extraction server error: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"local extraction server returned non-JSON: {exc}") from exc


# ---------------------------------------------------------------- validation

@dataclass
class ExtractionResult:
    documents: dict = field(default_factory=dict)
    evidence_map: dict = field(default_factory=dict)
    uncertain_fields: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    model_version: str = ""
    prompt_hash: str = ""

    @property
    def valid(self):
        return not self.violations


_TYPES = {
    "bool": lambda v: isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "date": lambda v: isinstance(v, str) and _is_iso_date(v),
    "list": lambda v: isinstance(v, list),
}


def _is_iso_date(value):
    try:
        date.fromisoformat(value[:10])
        return True
    except (ValueError, TypeError):
        return False


def validate_output(raw, schema, pages, threshold=None):
    """Strict post-hoc validation. Unknown keys are violations, not extra data."""
    threshold = schema.get("confidence_threshold", 0.75) if threshold is None else threshold
    types = schema["document_types"]
    result = ExtractionResult()
    result.prompt_hash = sha256_text(canonical({"schema_version": schema["schema_version"],
                                                "pages": pages}))
    if not isinstance(raw, dict):
        result.violations.append("response is not a JSON object")
        return result
    model_version = raw.get("model_version")
    if not isinstance(model_version, str) or not model_version.strip():
        result.violations.append("missing or invalid model_version")
    else:
        result.model_version = model_version
    docs = raw.get("documents")
    if not isinstance(docs, dict):
        result.violations.append("missing or invalid documents map")
        return result

    for doc_key, doc in docs.items():
        if doc_key not in types:
            result.violations.append(f"unknown document type returned by model: {doc_key}")
            continue
        if not isinstance(doc, dict):
            result.violations.append(f"{doc_key}: document payload is not an object")
            continue
        status = doc.get("status", "OK")
        if status != "OK":
            result.documents[doc_key] = {"status": "UNREADABLE"}
            result.uncertain_fields.append(f"{doc_key}: document unreadable")
            continue
        fields_raw = doc.get("fields")
        confidence = doc.get("confidence", {})
        evidence = doc.get("evidence", {})
        if not isinstance(fields_raw, dict) or not isinstance(confidence, dict):
            result.violations.append(f"{doc_key}: fields/confidence must be objects")
            continue
        unknown = set(fields_raw) - set(types[doc_key])
        if unknown:
            result.violations.append(f"{doc_key}: hallucinated fields {sorted(unknown)}")
            continue
        clean = {}
        for fname, ftype in types[doc_key].items():
            if fname not in fields_raw:
                continue
            value = fields_raw[fname]
            if not _TYPES[ftype](value):
                result.violations.append(
                    f"{doc_key}.{fname}: wrong type (expected {ftype}, got {type(value).__name__})")
                continue
            conf = confidence.get(fname, 0.0)
            if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0 <= conf <= 1:
                result.violations.append(f"{doc_key}.{fname}: invalid confidence value")
                continue
            if conf < threshold:
                result.uncertain_fields.append(f"{doc_key}.{fname}: confidence {conf:.2f} < {threshold}")
                continue
            clean[fname] = value
            ev = evidence.get(fname) if isinstance(evidence, dict) else None
            if isinstance(ev, dict):
                result.evidence_map[f"{doc_key}.{fname}"] = {
                    k: ev[k] for k in ("page", "bbox", "snippet") if k in ev
                }
        result.documents[doc_key] = clean
    return result


# ---------------------------------------------------------------- pipeline

def _abstain_verdict(conn, tx_id, reasons, result, actor_id):
    tx = get_tx(conn, tx_id)
    verdict = submit_verdict(conn, tx_id, {
        "status": "ABSTAIN", "decision": "ABSTAIN", "confidence": 0.0,
        "missing_documents": [], "invalid_documents": [],
        "notes": ["Extraction failed validation; no compliance determination was made",
                  f"violations: {canonical(reasons)}"],
        "evidence_spans": [], "rule_citations": [],
        "model_version": result.model_version or "unknown",
        "rules_engine_version": "rules-engine-1.0-no-llm",
        "ruleset_version": tx["ruleset_version"],
        "corpus_snapshot_id": tx["corpus_snapshot_id"],
        "prompt_hash": result.prompt_hash,
        "checklist_approved_by": "not-evaluated",
        "evaluated_at": now(),
    }, actor_id=actor_id, actor_role="EXTRACTION_ADAPTER")
    return verdict


def extract_and_check(conn, tx_id, checklist, pages, backend, schema=None,
                      ocr=None, threshold=None, actor_id="extraction-adapter", rag=None):
    """Full step-6 pipeline: gate -> OCR -> extract -> validate -> check."""
    if not has_event(conn, tx_id, "BID_OPENING_RECORDED"):
        append_event(conn, tx_id, "POLICY_BLOCKED", actor_id, "EXTRACTION_ADAPTER", {
            "attempt": "extract_and_check",
            "reason": "no BID_OPENING_RECORDED event; bid contents are sealed",
        })
        raise PolicyBlocked("extraction of bid contents is blocked until public bid opening")

    schema = schema or load_schema()
    ocr = ocr or PassthroughOCR()
    normalized = ocr.pages(pages)
    append_event(conn, tx_id, "OCR_COMPLETED", actor_id, "EXTRACTION_ADAPTER", {
        "page_count": len(normalized),
        "ocr_backend": type(ocr).__name__,
    })

    try:
        raw = backend.extract(normalized, schema)
    except ExtractionError as exc:
        append_event(conn, tx_id, "EXTRACTION_FAILED", actor_id, "EXTRACTION_ADAPTER",
                     {"reason": str(exc)})
        raise

    result = validate_output(raw, schema, normalized, threshold=threshold)
    append_event(conn, tx_id,
                 "EXTRACTION_COMPLETED" if result.valid else "SCHEMA_VALIDATION_FAILED",
                 actor_id, "EXTRACTION_ADAPTER", {
                     "model_version": result.model_version,
                     "prompt_hash": result.prompt_hash,
                     "document_count": len(result.documents),
                     "uncertain_fields": result.uncertain_fields,
                     "violations": result.violations,
                 })

    if not result.valid:
        return _abstain_verdict(conn, tx_id, result.violations, result, actor_id), result

    extracted = {"as_of": get_tx(conn, tx_id)["posting_date"],
                 "documents": result.documents}
    meta = {"extraction_model_version": result.model_version,
            "prompt_hash": result.prompt_hash}
    verdict = evaluate_submission(
        conn, tx_id, checklist, extracted,
        evidence_map=result.evidence_map, extraction_meta=meta,
        citation_resolver=rag.resolve_citation if rag else None,
    )
    if rag:
        append_event(conn, tx_id, "RAG_CITATIONS_RESOLVED", actor_id, "RAG", {
            "citation_count": len(verdict["rule_citations"]),
            "verified_count": sum(1 for c in verdict["rule_citations"] if c.get("verified")),
            "corpus_snapshot_id": get_tx(conn, tx_id)["corpus_snapshot_id"],
        })
    return verdict, result


def extract_pdf(conn, tx_id, checklist, pdf_path, backend, *,
                language="eng", dpi=300, poppler=("pdftoppm",),
                tesseract=("tesseract",), schema=None, threshold=None,
                actor_id="extraction-adapter", rag=None,
                workdir=None, keep_workdir=False):
    """Render a local PDF -> OCR -> typed extraction with fail-closed validation.

    The OCR step runs through ``extract_and_check`` so the ``OCR_COMPLETED``
    ledger event (and downstream provenance) is recorded exactly as for the
    digital-PDF path. Bid-content gate applies (extraction before public opening
    is blocked). Operator dependencies: Poppler (``pdftoppm``) and tesseract.
    """
    owned = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="procemp-ocr-")
    os.makedirs(workdir, exist_ok=True)
    try:
        images = render_pdf_to_images(pdf_path, workdir, dpi=dpi,
                                      poppler=poppler)
        pages = [{"image_path": path} for path in images]
        ocr = TesseractOCR(language=language, tesseract=tesseract)
        return extract_and_check(conn, tx_id, checklist, pages, backend,
                                 schema=schema, ocr=ocr, threshold=threshold,
                                 actor_id=actor_id, rag=rag)
    finally:
        if owned and not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def valid_stub_response(model_version="qwen2.5-3b-ft-procurement-v1"):
    """A clean model response, used by tests and the demo."""
    return {
        "model_version": model_version,
        "documents": {
            "philgeps_registration": {
                "status": "OK",
                "fields": {"present": True, "valid_until": "2027-06-30"},
                "confidence": {"present": 0.99, "valid_until": 0.93},
                "evidence": {"present": {"page": 1, "snippet": "PhilGEPS Platinum"},
                             "valid_until": {"page": 1, "snippet": "valid until 2027-06-30"}},
            },
            "omnibus_sworn_statement": {
                "status": "OK",
                "fields": {"present": True, "signed": True, "notarized": True},
                "confidence": {"present": 0.99, "signed": 0.96, "notarized": 0.88},
                "evidence": {"notarized": {"page": 4, "snippet": "notarial seal"}},
            },
            "bid_securing_declaration": {
                "status": "OK",
                "fields": {"present": True, "signed": True},
                "confidence": {"present": 0.98, "signed": 0.95},
                "evidence": {},
            },
            "financial_proposal": {
                "status": "OK",
                "fields": {"present": True, "amount": 950000},
                "confidence": {"present": 0.99, "amount": 0.97},
                "evidence": {"amount": {"page": 2, "snippet": "PHP 950,000.00"}},
            },
        },
    }


def _load_checklist(path):
    if not path:
        raise ExtractionError("--checklist is required (named BAC officer approval required)")
    if not os.path.isfile(path):
        raise ExtractionError(f"checklist file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"checklist is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not data.get("approved_by"):
        raise ExtractionError("checklist must carry approved_by (a named BAC Secretariat officer)")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ProcEMP step 6: render PDF -> OCR -> typed extraction (fail closed)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ocr", help="render a scanned PDF, OCR it, then typed extraction")
    p.add_argument("--db", required=True)
    p.add_argument("--tx-id", required=True)
    p.add_argument("--checklist", required=True,
                   help="BAC-approved checklist JSON (must carry approved_by)")
    p.add_argument("--file", required=True, help="local PDF path (never a URL)")
    p.add_argument("--language", default="eng", help="e.g. eng, fil, or eng+fil")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--url", help="local loopback model endpoint (e.g. http://127.0.0.1:8080/extract)")
    p.add_argument("--model-version", default="qwen2.5-3b-ft-procurement-v1")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--stub", action="store_true",
                   help="dry run with a built-in stub response; NO model is called")
    args = parser.parse_args(argv)

    if args.cmd == "ocr":
        try:
            checklist = _load_checklist(args.checklist)
            if args.stub:
                backend = StubBackend(valid_stub_response(args.model_version))
                dry_run = True
            elif args.url:
                backend = LocalHTTPBackend(args.url, model_version=args.model_version,
                                           timeout=args.timeout)
                dry_run = False
            else:
                raise ExtractionError(
                    "an inference backend is required: --url (loopback) or --stub (dry run)"
                )
            conn = _connect(args.db)
            try:
                verdict, result = extract_pdf(
                    conn, args.tx_id, checklist, args.file, backend,
                    language=args.language, dpi=args.dpi,
                )
            finally:
                conn.close()
        except CoreError as exc:
            print(json.dumps({"error": str(exc), "advisory_only": True}))
            return 1
        out = {
            "advisory_only": True,
            "dry_run": dry_run,
            "note": ("no model was called" if args.stub else
                     "typed extraction via local loopback model; still advisory — "
                     "human bodies make statutory decisions"),
            "verdict": verdict,
            "validation": {
                "valid": result.valid,
                "violations": result.violations,
                "uncertain_fields": result.uncertain_fields,
                "model_version": result.model_version,
                "prompt_hash": result.prompt_hash,
            },
            "operator_dependencies": [
                "poppler (pdftoppm) for PDF rendering",
                "tesseract with eng (and optional fil) language data",
            ],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
