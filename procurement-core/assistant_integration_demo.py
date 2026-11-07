"""End-to-end assistant integration demo (no real model weights required).

Demonstrates the ProcEMP decision-support flow:
1. Start a loopback extraction stub server (contract-compatible with
   extraction.LocalHTTPBackend).
2. Run OCR/extraction -> strict validation -> deterministic checker -> RAG.
3. Verdicts are advisory; ABSTAIN/FAIL are auto-enqueued to the BAC review queue.
4. Export a review-queue HTML dashboard and a schema-validated SFT JSONL stub
   record (training scaffolding).

This proves the integration path, not that a production Qwen2.5-3B model is
trained. Real training requires approved/anonymized data, a QLoRA job, frozen
benchmark evaluation, and human-gated promotion (see learning.py, plan.md).
"""

from __future__ import annotations

import json
import os
import threading
from http.server import ThreadingHTTPServer

from checker import sample_checklist
from core import (
    add_signoff, create_transaction, init_db, now, record_bid_opening,
    request_transition, sha256_text,
)
from extraction import LocalHTTPBackend, extract_and_check, valid_stub_response
from rag import AsOfRAG
from review_queue import export_queue_dashboard, list_queue
from training_adapter import _StubHandler, build_sft_record, load_schema, write_jsonl


def _start_stub_server(responder):
    handler = type("Handler", (_StubHandler,), {"responder": staticmethod(responder)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/extract"


def main():
    conn = init_db(":memory:")
    schema = load_schema()

    # 1) Loopback extraction stub (replace with real local Qwen2.5-3B server later).
    def responder(req):
        resp = valid_stub_response(req.get("model_version", "qwen2.5-3b-ft-procurement-stub"))
        # Simulate an uncertain field on one run by lowering confidence only when asked.
        if req.get("options", {}).get("simulate_uncertainty"):
            resp = json.loads(json.dumps(resp))
            resp["documents"]["omnibus_sworn_statement"]["confidence"]["notarized"] = 0.40
        return resp

    server, url = _start_stub_server(responder)
    try:
        backend = LocalHTTPBackend(url=url, model_version="qwen2.5-3b-ft-procurement-stub")

        # 2) Transaction + custody/opening gates (required before reading bid contents).
        tx = create_transaction(
            conn, regime="RA12009_IRR", posting_date="2026-11-05",
            mode="Small Value Procurement", award_criterion="LCRB",
            drafter_id="staff-01",
            legal_basis_note="Demo: RA 12009 applies (legal officer confirmation required in production)",
        )
        request_transition(conn, tx, "OPENING_SCHEDULED", sha256_text("receipt-v1"))
        record_bid_opening(conn, tx, "bac-chair-01", "minutes-demo",
                           ["TECHNICAL_ENVELOPE"], ["bac-chair-01", "observer-01"])
        add_signoff(conn, tx, "OPENING_SCHEDULED", "CONTENT_INGESTION",
                    "bac-chair-01", "BAC_CHAIR", sha256_text("minutes-demo"))
        request_transition(conn, tx, "CONTENT_INGESTION", sha256_text("minutes-demo"))
        add_signoff(conn, tx, "CONTENT_INGESTION", "EVALUATION",
                    "bac-chair-01", "BAC_CHAIR", sha256_text("eval-v1"))
        request_transition(conn, tx, "EVALUATION", sha256_text("eval-v1"))

        checklist = sample_checklist(conn.execute(
            "SELECT ruleset_version FROM proc_tx WHERE id=?", (tx,)).fetchone()[0])
        pages = [
            {"page": 1, "text": "PhilGEPS Platinum Registration, valid until 2027-06-30"},
            {"page": 2, "text": "Financial proposal: PHP 950,000.00"},
            {"page": 4, "text": "Omnibus Sworn Statement, signed and notarized"},
        ]
        rag = AsOfRAG(conn)

        # 3) Advisory extraction+check. ABSTAIN/FAIL auto-enqueue to review queue.
        verdict, extraction = extract_and_check(conn, tx, checklist, pages, backend, rag=rag)
        print("Assistant verdict (advisory):", verdict["decision"], verdict.get("verdict_id"))
        print("Queue items:", list_queue(conn)["count"])

        # 4) Export artifacts.
        export_queue_dashboard(conn, "assistant-review-queue.html")
        record = build_sft_record(
            pages=pages,
            target_response=valid_stub_response(backend.model_version),
            schema=schema,
            model_version=backend.model_version,
            metadata={"source": "assistant_integration_demo", "generated_at": now(),
                      "note": "Stub target; replace with adjudicated/approved training data only."},
        )
        write_jsonl([record], "assistant_sft_stub.jsonl")
        print("Wrote assistant-review-queue.html and assistant_sft_stub.jsonl")
        print("Model training active?", "NO - this demo uses a stub responder; no QLoRA/training job ran.")
    finally:
        server.shutdown()
        server.server_close()
        conn.close()


if __name__ == "__main__":
    main()