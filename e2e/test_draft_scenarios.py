"""Playwright E2E tests — ProcEMP drafting form (/draft), all scenarios.

DEV-ONLY dependency: the application itself stays stdlib-only; this harness
lives outside procurement-core/tests/ and needs:

    pip install playwright
    python -m playwright install chromium

Covered scenarios (the same ones suggested for the BAC demo):
  1. full chain        — defaults (3 suppliers + award) -> 6 watermarked PDFs
  2. partial chain     — suppliers cleared -> PR + RFQ only (pre-canvass)
  3. fail-closed award — award to the HIGHEST bidder -> rendering refused,
                         violations shown, zero PDFs written for that request
  4. missing fields    — purpose cleared -> "missing required fields" error
  5. download          — a /draft-file link serves real PDF bytes with
                         Content-Disposition attachment
  6. auth              — with a token configured: no token -> 401 page;
                         ?token= -> the form

Run:  python e2e/test_draft_scenarios.py
The harness starts its own review server(s) on ephemeral ports with temp
databases — it never touches procurement.db or your running services.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(BASE, "procurement-core")

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    print("Playwright is not installed. Dev-only dependency:\n"
          "  pip install playwright\n  python -m playwright install chromium")
    sys.exit(2)


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(db, drafts, port, token=None):
    # The DB must have the schema BEFORE the server starts: review_server
    # opens the SQLite file directly (health works on an empty file, but
    # generation would fail with "no such table").
    sys.path.insert(0, CORE)
    from core import init_db
    init_db(db).close()
    argv = [sys.executable, "review_server.py", "--db", db,
            "--drafts-dir", drafts, "--port", str(port)]
    if token:
        argv += ["--auth-token", token]
    log = open(os.path.join(tempfile.gettempdir(), f"procemp-e2e-{port}.log"), "w")
    proc = subprocess.Popen(argv, cwd=CORE, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    return proc
        except Exception:
            if proc.poll() is not None:  # died before becoming healthy
                break
            time.sleep(0.2)
    proc.kill()
    log.close()
    tail = open(os.path.join(tempfile.gettempdir(),
                             f"procemp-e2e-{port}.log")).read()[-400:]
    raise RuntimeError(f"server on :{port} did not become healthy; log:\n{tail}")


class DraftFormE2E(unittest.TestCase):
    """Open server (no token) — all drafting scenarios."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="procemp-e2e-")
        cls.drafts = os.path.join(cls.tmp, "drafts")
        cls.port = free_port()
        cls.proc = start_server(os.path.join(cls.tmp, "e2e.db"),
                                cls.drafts, cls.port)
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.page = cls.browser.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _open_form(self):
        self.page.goto(f"{self.base}/draft")
        expect(self.page.get_by_text("Generate draft chain")).to_be_visible()
        # Actor ID is required (audit) and empty by default — the browser's
        # native validation silently blocks submit until it is filled, which
        # is exactly what a real operator does before generating.
        self.page.fill('input[name="actor_id"]', "e2e-operator-01")

    def _generate(self):
        self.page.get_by_role("button",
                              name="Generate draft chain (6 PDFs)").click()

    def _pdf_links(self):
        return self.page.locator('a[href^="/draft-file"]')

    def test_1_full_chain_from_defaults(self):
        self._open_form()
        self._generate()
        expect(self.page.get_by_text("Draft chain generated")).to_be_visible()
        self.assertEqual(self._pdf_links().count(), 6)
        # ledger-backed numbering in the results heading
        self.assertIn("RO2-PR-", self.page.locator("h2").first.inner_text())
        # advisory notes: 3 suppliers recorded -> no Sec.34 note expected
        self.assertEqual(self.page.get_by_text("only 1 quotation").count(), 0)

    def test_2_partial_chain_without_suppliers(self):
        self._open_form()
        for i in range(1, 5):
            self.page.fill(f'input[name="sup_name_{i}"]', "")
            self.page.fill(f'input[name="sup_bid_{i}"]', "")
        self._generate()
        expect(self.page.get_by_text("Draft chain generated")).to_be_visible()
        self.assertEqual(self._pdf_links().count(), 2)  # PR + RFQ only
        names = [self._pdf_links().nth(i).inner_text()
                 for i in range(self._pdf_links().count())]
        self.assertTrue(any("purchase-request" in n for n in names))
        self.assertTrue(any("request-for-quotation" in n for n in names))
        self.assertFalse(any("purchase-order" in n for n in names))

    def test_3_refuses_award_to_highest_bidder(self):
        self._open_form()
        self.page.fill('input[name="award_supplier"]', "SAMPLE SUPPLIER C")
        self._generate()
        # server refuses to render; error lands back on the form page
        expect(self.page.get_by_text("chain failed validation")).to_be_visible()
        expect(self.page.get_by_text("lowest calculated bid")).to_be_visible()
        # no results panel for the tampered request
        self.assertEqual(self.page.get_by_text("Draft chain generated").count(), 0)

    def test_4_missing_required_fields_fail_closed(self):
        # purpose is a required input: the browser's native validation blocks
        # submission (no navigation at all) — the operator stays on the form.
        self._open_form()
        self.page.fill('input[name="purpose"]', "")
        self._generate()
        self.assertTrue(self.page.url.endswith("/draft"),
                        f"expected no navigation, got {self.page.url}")
        expect(self.page.get_by_text("Generate draft chain")).to_be_visible()
        self.assertEqual(self._pdf_links().count(), 0)

    def test_5_download_serves_real_pdf(self):
        self._open_form()
        self._generate()
        expect(self.page.get_by_text("Draft chain generated")).to_be_visible()
        link = self._pdf_links().first
        href = link.get_attribute("href")
        resp = self.page.request.get(self.base + href)
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.body().startswith(b"%PDF"))
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))


class DraftAuthE2E(unittest.TestCase):
    """Token-configured server: 401 without token, form with ?token=."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="procemp-e2e-auth-")
        cls.port = free_port()
        cls.proc = start_server(os.path.join(cls.tmp, "auth.db"),
                                os.path.join(cls.tmp, "drafts"),
                                cls.port, token="e2e-secret-token")
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.page = cls.browser.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_token_is_denied(self):
        self.page.goto(f"{self.base}/draft")
        expect(self.page.get_by_text("Unauthorized")).to_be_visible()

    def test_query_token_grants_access(self):
        self.page.goto(f"{self.base}/draft?token=e2e-secret-token")
        expect(self.page.get_by_text("Generate draft chain")).to_be_visible()

    def test_wrong_token_is_denied(self):
        self.page.goto(f"{self.base}/draft?token=wrong-token")
        expect(self.page.get_by_text("Unauthorized")).to_be_visible()


if __name__ == "__main__":
    unittest.main(verbosity=2)
