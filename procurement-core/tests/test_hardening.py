"""Tests for phase-2 hardening: COA auth, TF-IDF, worklist, probe, viewers.

- coa_server.py authenticated access (token header/query, /health open)
- rag.py TfidfEmbedding (fit/embed determinism + retrieval wiring)
- readiness_report.py worklist generator
- model_plumbing_check.py loopback runtime probe (no data sent)
- review_server.py richer viewers (documents/citations/ledger + role hints)
"""

import hashlib
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import CoreError, init_db, resolve_rules, create_transaction, submit_verdict, sha256_text, request_transition, record_bid_opening, add_signoff, ingest_document


def new_tx(conn, **kw):
    params = dict(
        regime="RA12009_IRR", posting_date="2026-11-05",
        mode="Competitive Bidding", award_criterion="LCRB",
        drafter_id="staff-01",
        legal_basis_note="Legal officer memo: RA 12009 applies",
    )
    params.update(kw)
    return create_transaction(conn, **params)


class CoaAuthTests(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_open_by_default(self):
        from coa_server import handle
        status, _ = handle(self.conn, "/verify-chain")
        self.assertEqual(status, 200)
        status, _ = handle(self.conn, "/verify-folder")
        self.assertEqual(status, 400)

    def test_token_required_when_configured(self):
        from coa_server import handle
        status, payload = handle(self.conn, "/verify-chain", auth_token="secret")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")
        status, _ = handle(self.conn, "/health", auth_token="secret")
        self.assertEqual(status, 200)

    def test_token_via_header_and_query(self):
        from coa_server import handle
        status, _ = handle(self.conn, "/verify-chain", auth_token="secret",
                           headers={"X-Auth-Token": "secret"})
        self.assertEqual(status, 200)
        status, _ = handle(self.conn, "/verify-chain?token=secret", auth_token="secret")
        self.assertEqual(status, 200)
        status, _ = handle(self.conn, "/verify-chain?token=wrong", auth_token="secret")
        self.assertEqual(status, 401)
        status, _ = handle(self.conn, "/verify-chain?auth_token=secret", auth_token="secret")
        self.assertEqual(status, 200)


class TfidfTests(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        self.tx = new_tx(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_embed_deterministic_and_normalized(self):
        from rag import TfidfEmbedding
        emb = TfidfEmbedding()
        vec = emb.embed("confidentiality of submitted bids")
        self.assertEqual(len(vec), 128)
        self.assertAlmostEqual(sum(v * v for v in vec) ** 0.5, 1.0, places=6)
        self.assertEqual(vec, emb.embed("confidentiality of submitted bids"))

    def test_retrieval_with_fitted_tfidf(self):
        from rag import AsOfRAG, TfidfEmbedding
        rows = self.conn.execute(
            "SELECT text FROM corpus_chunk WHERE verified=1"
        ).fetchall()
        emb = TfidfEmbedding().fit([r["text"] for r in rows])
        rag = AsOfRAG(self.conn, emb)
        hits = rag.retrieve("confidentiality of submitted bids", self.tx, top_k=2)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["section"], "58.5")


class WorklistTests(unittest.TestCase):
    def test_worklist_renders_sections_and_unverified(self):
        from readiness_report import build_report, render_worklist
        report = build_report()
        text = render_worklist(report)
        self.assertIn("Readiness Worklist", text)
        self.assertIn("Transition policy", text)
        self.assertIn("`rs-ra9184-2016`", text)
        self.assertIn("LEGACY-ANNEX-H-PLACEHOLDER", text)
        self.assertIn("36 forms total", text)
        self.assertIn("HUMAN/EXTERNAL", text)
        # generator never marks unconfirmed policy as done
        self.assertIn("Do not auto-select", text)


class ProbeTests(unittest.TestCase):
    def test_probe_no_runtimes(self):
        from model_plumbing_check import probe_runtimes
        result = probe_runtimes(timeout=1)
        self.assertTrue(result["advisory_only"])
        self.assertFalse(result["data_sent"])
        # default loopback probes (ollama, lm_studio, llama_cpp x2, vllm,
        # procemp_adapter); env may or may not have runtimes -> only shape
        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 6)

    def test_probe_refuses_non_loopback(self):
        from model_plumbing_check import _assert_loopback_url
        with self.assertRaises(ValueError):
            _assert_loopback_url("http://192.168.1.50:8080/health")
        _assert_loopback_url("http://127.0.0.1:11434/api/tags")

    def test_probe_detects_ollama_stub(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"models": [{"model": "qwen2.5:3b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        from model_plumbing_check import probe_runtimes
        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe_runtimes(timeout=5, probes=[("ollama", f"http://127.0.0.1:{port}/api/tags")])
            self.assertEqual(result["reachable_count"], 1)
            self.assertEqual(result["results"][0]["detail"]["models"], ["qwen2.5:3b"])
        finally:
            server.shutdown()
            server.server_close()


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        self.tx = new_tx(self.conn)
        from rag import AsOfRAG
        rag = AsOfRAG(self.conn)
        citation = rag.resolve_citation("IRR RA 12009 Sec. 59.1", self.tx)
        self.assertTrue(citation["verified"])
        r = resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")
        self.verdict = submit_verdict(self.conn, self.tx, dict(
            status="FAIL", decision="FAIL", confidence=0.95,
            missing_documents=["bid_securing_declaration"], invalid_documents=[],
            notes=["advisory test"],
            evidence_spans=[{"field": "financial_proposal.amount", "value": 950000,
                            "page": 2, "snippet": "PHP 950,000.00"}],
            rule_citations=[citation],
            model_version="qwen2.5-3b-ft-test",
            ruleset_version=r["ruleset_version"],
            corpus_snapshot_id=r["corpus_snapshot_id"],
            prompt_hash=sha256_text("prompt-v1"),
        ), actor_id="checker")
        from review_queue import list_queue
        self.item_id = list_queue(self.conn)["items"][0]["id"]

    def tearDown(self):
        self.conn.close()

    def test_render_item_has_viewers(self):
        from review_server import render_item
        html = render_item(self.conn, self.item_id)
        self.assertIn("Rule citations (passage + verification)", html)
        self.assertIn("Ingested documents", html)
        self.assertIn("Ledger timeline", html)
        self.assertIn("datalist", html)
        self.assertIn("Allowed:", html)

    def test_citations_table_verifies(self):
        from review_server import _citations_table
        from rag import AsOfRAG
        rag = AsOfRAG(self.conn)
        citation = rag.resolve_citation("IRR RA 12009 Sec. 59.1", self.tx)
        html = _citations_table(self.conn, self.tx, [citation])
        self.assertIn(citation["chunk_id"], html)
        self.assertIn("verified", html)

    def test_adjudicate_form_role_enforced(self):
        from review_server import handle_post
        # override to create a correction
        from review_queue import submit_override_correction
        corr = submit_override_correction(
            self.conn, self.item_id, field_path="documents.philgeps.valid_until",
            ai_value="2025-01-01", correct_value="2027-01-01",
            reason="test", submitted_by="staff-01")
        item_id, msg = handle_post(self.conn, {
            "action": ["adjudicate"], "item_id": [self.item_id],
            "correction_id": [corr["correction_id"]], "decision": ["ACCEPTED"],
            "adjudicator": ["reviewer-02"], "actor_role": ["SECOND_REVIEWER"],
            "note": [""],
        })
        self.assertIn("adjudicated", msg)
        # wrong role must raise
        corr2 = submit_override_correction(
            self.conn, self.item_id, field_path="documents.philgeps.valid_until",
            ai_value="2025-01-01", correct_value="2027-01-01",
            reason="test2", submitted_by="staff-01")
        with self.assertRaises(CoreError):
            handle_post(self.conn, {
                "action": ["adjudicate"], "item_id": [self.item_id],
                "correction_id": [corr2["correction_id"]], "decision": ["ACCEPTED"],
                "adjudicator": ["staff-02"], "actor_role": ["BAC_STAFF"],
                "note": [""],
            })


class TlsAndHeaderTests(unittest.TestCase):
    def test_context_none_when_unconfigured(self):
        from ops_tls import build_tls_context, scheme_for, wrap_server
        self.assertIsNone(build_tls_context())
        self.assertEqual(scheme_for(None), "http")
        sentinel = object()
        self.assertIs(wrap_server(sentinel, None), sentinel)

    def test_half_configured_tls_fails_closed(self):
        from ops_tls import build_tls_context
        with self.assertRaises(ValueError):
            build_tls_context("cert.pem", None)
        with self.assertRaises(ValueError):
            build_tls_context(None, "key.pem")
        with self.assertRaises(ValueError):
            build_tls_context(None, None, "ca.pem")

    def test_security_headers(self):
        from ops_tls import add_security_headers
        collected = []
        add_security_headers(lambda k, v: collected.append((k, v)))
        names = {k for k, _ in collected}
        self.assertIn("X-Content-Type-Options", names)
        self.assertIn("X-Frame-Options", names)
        self.assertIn("Content-Security-Policy", names)
        self.assertNotIn("Strict-Transport-Security", names)
        collected.clear()
        add_security_headers(lambda k, v: collected.append((k, v)), tls=True)
        self.assertIn("Strict-Transport-Security", {k for k, _ in collected})

    def test_tls_cli_flags_and_env_fallback(self):
        import argparse
        from ops_tls import add_tls_arguments
        parser = argparse.ArgumentParser()
        add_tls_arguments(parser, env_prefix="PROCEMP_TEST_NONEXISTENT")
        args = parser.parse_args([])
        self.assertIsNone(args.tls_cert)
        self.assertIsNone(args.tls_key)
        self.assertIsNone(args.tls_client_ca)
        args = parser.parse_args(["--tls-cert", "c.pem", "--tls-key", "k.pem"])
        self.assertEqual(args.tls_cert, "c.pem")
        self.assertEqual(args.tls_key, "k.pem")

    def _fetch_headers(self, handler, path):
        import urllib.request
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                return resp.status, resp.headers
        finally:
            server.shutdown()
            server.server_close()

    def test_review_ui_sends_defensive_headers(self):
        from review_server import make_handler
        conn = init_db(":memory:")
        try:
            status, headers = self._fetch_headers(make_handler(conn, tls=True), "/health")
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("Strict-Transport-Security", headers)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        finally:
            conn.close()

    def test_coa_server_sends_defensive_headers(self):
        from coa_server import make_handler as coa_handler
        conn = init_db(":memory:")
        try:
            status, headers = self._fetch_headers(coa_handler(conn), "/health")
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertNotIn("Strict-Transport-Security", headers)
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        finally:
            conn.close()


class SmokeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "smoke.db")
        conn = init_db(self.db)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_smoke_all_read_only_checks_pass(self):
        from ops_smoke import run_smoke
        result = run_smoke(self.db)
        self.assertTrue(result["ok"])
        self.assertFalse(result["decision_making"])
        self.assertEqual([c["name"] for c in result["checks"]],
                         ["ledger_chain", "corpus_integrity", "readiness"])
        readiness = result["checks"][-1]
        self.assertEqual(readiness["severity"], "info")
        # Blockers are advisory in the smoke result, never silently release-ready.
        self.assertFalse(readiness["detail"]["ready_for_production"])
        self.assertTrue(readiness["detail"]["blockers"])

    def test_smoke_refuses_missing_database(self):
        from ops_smoke import run_smoke
        missing = os.path.join(self.tmp.name, "does-not-exist.db")
        result = run_smoke(missing)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_checks"], ["database"])
        # read-only promise: the missing file must not be created
        self.assertFalse(os.path.exists(missing))

    def test_smoke_refuses_non_loopback_health(self):
        from ops_smoke import run_smoke
        result = run_smoke(self.db, review_url="http://192.168.1.50:8091/health")
        self.assertFalse(result["ok"])
        self.assertIn("review_ui_health", result["failed_checks"])

    def test_smoke_polls_live_review_health(self):
        from core import _connect
        from ops_smoke import run_smoke
        from review_server import make_handler
        conn = _connect(self.db)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(conn))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_smoke(self.db, review_url=f"http://127.0.0.1:{port}/health")
            self.assertTrue(result["ok"])
            names = [c["name"] for c in result["checks"]]
            self.assertIn("review_ui_health", names)
        finally:
            server.shutdown()
            server.server_close()
            conn.close()


class TransactionContextValidationTests(unittest.TestCase):
    """Fail closed at transaction creation: dates, mode, award criterion, stage."""

    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_rejects_unknown_mode(self):
        with self.assertRaises(CoreError):
            new_tx(self.conn, mode="Shopping")

    def test_rejects_mode_outside_verified_list(self):
        with self.assertRaises(CoreError):
            new_tx(self.conn, mode="Competitive Biddding")  # typo must not pass

    def test_rejects_unknown_award_criterion(self):
        with self.assertRaises(CoreError):
            new_tx(self.conn, award_criterion="LOWEST_PRICE_ONLY")

    def test_rejects_bad_posting_date(self):
        for value in ("2026-13-01", "not-a-date", "2026-11-31"):
            with self.subTest(value=value), self.assertRaises(CoreError):
                new_tx(self.conn, posting_date=value)

    def test_rejects_missing_drafter_and_unknown_stage(self):
        with self.assertRaises(CoreError):
            new_tx(self.conn, drafter_id="")
        with self.assertRaises(CoreError):
            new_tx(self.conn, stage="FLYING")

    def test_accepts_verified_mode_and_criterion(self):
        tx = new_tx(self.conn, mode="Small Value Procurement", award_criterion="LCRB")
        row = self.conn.execute(
            "SELECT mode, award_criterion, posting_date FROM proc_tx WHERE id=?",
            (tx,)).fetchone()
        self.assertEqual(row["mode"], "Small Value Procurement")
        self.assertEqual(row["award_criterion"], "LCRB")
        self.assertEqual(row["posting_date"], "2026-11-05")

    def test_datetime_is_normalized_to_date(self):
        from datetime import datetime
        tx = new_tx(self.conn, posting_date=datetime(2026, 11, 5, 13, 30))
        row = self.conn.execute("SELECT posting_date FROM proc_tx WHERE id=?", (tx,)).fetchone()
        self.assertEqual(row["posting_date"], "2026-11-05")

    def test_legacy_unverified_rules_do_not_block_creation(self):
        # Legacy rules fail closed on READ (UnverifiedRuleError); creating the
        # transaction records the legal officer's explicit regime choice.
        tx = new_tx(self.conn, regime="RA9184_2016IRR", posting_date="2023-06-01",
                    mode="Shopping", award_criterion=None)
        self.assertTrue(tx)


class PostLimitTests(unittest.TestCase):
    def test_oversized_post_body_is_rejected(self):
        import http.client
        from review_server import MAX_POST_BYTES, make_handler
        conn = init_db(":memory:")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(conn))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            client.putrequest("POST", "/action")
            client.putheader("Content-Length", str(MAX_POST_BYTES + 1))
            client.endheaders()
            resp = client.getresponse()
            resp.read()
            self.assertEqual(resp.status, 413)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            conn.close()

    def test_unknown_post_path_is_400_without_reading_body(self):
        import http.client
        from review_server import make_handler
        conn = init_db(":memory:")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(conn))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            client.putrequest("POST", "/nope")
            client.putheader("Content-Length", "0")
            client.endheaders()
            resp = client.getresponse()
            resp.read()
            self.assertEqual(resp.status, 400)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            conn.close()


class _EmbeddingStubHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            inputs = body.get("input")
            items = [inputs] if isinstance(inputs, str) else list(inputs or [])
            data = []
            for i, text in enumerate(items):
                vec = [((int(hashlib.sha256(f"{text}:{j}".encode()).hexdigest(), 16) % 2000) - 1000) / 1000.0
                       for j in range(8)]
                data.append({"index": i, "embedding": vec})
            raw = json.dumps({"model": body.get("model"), "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except Exception:
            self.send_response(500)
            self.end_headers()

    def log_message(self, *args):
        pass


class _BadEmbeddingShapeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = json.dumps({"data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def _start_embedding_stub(handler_cls):
    handler_cls.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/v1/embeddings"
    return server, url


class HTTPEmbeddingTests(unittest.TestCase):
    def test_embed_shape_determinism_and_batch(self):
        from rag import HTTPEmbedding
        server, url = _start_embedding_stub(_EmbeddingStubHandler)
        try:
            backend = HTTPEmbedding(url=url, model="test-embed")
            vec = backend.embed("confidentiality of submitted bids")
            self.assertEqual(len(vec), 8)
            self.assertTrue(all(isinstance(v, float) for v in vec))
            again = HTTPEmbedding(url=url, model="test-embed")
            self.assertEqual(again.embed("confidentiality of submitted bids"), vec)
            vecs = backend.embed_batch(["a", "b", "c"])
            self.assertEqual(len(vecs), 3)
            self.assertTrue(all(len(v) == 8 for v in vecs))
        finally:
            server.shutdown()
            server.server_close()

    def test_embed_caches_vectors(self):
        from rag import HTTPEmbedding
        server, url = _start_embedding_stub(_EmbeddingStubHandler)
        try:
            backend = HTTPEmbedding(url=url, model="test-embed")
            backend.embed("cache me")
            backend.embed("cache me")
            self.assertEqual(type(backend).__name__, "HTTPEmbedding")
            self.assertEqual(_EmbeddingStubHandler.calls, 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_non_loopback_endpoint_is_refused(self):
        from rag import HTTPEmbedding
        with self.assertRaises(CoreError):
            HTTPEmbedding(url="http://192.168.1.20:1234/v1/embeddings")
        with self.assertRaises(CoreError):
            HTTPEmbedding(url="ftp://127.0.0.1:1234/embeddings")

    def test_server_error_raises_core_error(self):
        from rag import HTTPEmbedding
        backend = HTTPEmbedding(url="http://127.0.0.1:1/v1/embeddings", timeout=1)
        with self.assertRaises(CoreError):
            backend.embed("x")

    def test_unexpected_vector_count_is_rejected(self):
        from rag import HTTPEmbedding
        server, url = _start_embedding_stub(_BadEmbeddingShapeHandler)
        try:
            backend = HTTPEmbedding(url=url, model="test-embed")
            with self.assertRaises(CoreError):
                backend.embed("x")
        finally:
            server.shutdown()
            server.server_close()

    def test_retrieve_wires_http_embedding(self):
        from rag import AsOfRAG, HTTPEmbedding
        conn = init_db(":memory:")
        try:
            tx = new_tx(conn)
            server, url = _start_embedding_stub(_EmbeddingStubHandler)
            try:
                rag = AsOfRAG(conn, HTTPEmbedding(url=url, model="test-embed"))
                hits = rag.retrieve("IRR RA 12009 Sec. 59.1 checklist of required documents", tx, top_k=2)
                self.assertTrue(hits)
                self.assertIn("chunk_id", hits[0])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
