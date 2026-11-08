"""Step 6 OCR path: local PDF rendering + fail-closed typed extraction.

Uses locally installed/optional binaries (Poppler ``pdftoppm`` + tesseract),
stubbed here with small Python fakes. No network, no model weights, no
confidential data — public behaviour only.
"""

import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (
    CoreError, init_db, create_transaction, record_bid_opening,
    resolve_rules,
)
from checker import sample_checklist
from extraction import (
    ExtractionError, extract_pdf, render_pdf_to_images,
    TesseractOCR, StubBackend, valid_stub_response,
)

FAKE_POPPLER = "impair_fake_pdftoppm.py"
FAKE_TESSERACT = "impair_fake_tesseract.py"


def make_fake_pdftoppm(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "import os,sys\n"
            "args = sys.argv[1:]\n"
            "out_prefix = args[-1]\n"
            "for n in (1, 2, 10):\n"
            "    with open(out_prefix + '-' + str(n) + '.png', 'wb') as f:\n"
            "        f.write(b'\\x89PNG\\r\\n\\x1a\\n' + bytes([n]))\n"
        )


def make_fake_tesseract(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "import os, re, sys\n"
            "args = sys.argv[1:]\n"
            "m = re.search(r'-(\\d+)\\.', os.path.basename(args[0]))\n"
            "page = m.group(1) if m else '?'\n"
            "print('page %s OCR text (eng)' % page)\n"
        )


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.outdir = os.path.join(cls.tmp.name, "out")
        os.makedirs(cls.outdir, exist_ok=True)
        cls.pdf = os.path.join(cls.tmp.name, "scan.pdf")
        with open(cls.pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 fake source")
        cls.poppler = os.path.join(cls.tmp.name, FAKE_POPPLER)
        make_fake_pdftoppm(cls.poppler)
        cls.tesseract = os.path.join(cls.tmp.name, FAKE_TESSERACT)
        make_fake_tesseract(cls.tesseract)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_render_orders_pages(self):
        images = render_pdf_to_images(
            self.pdf, self.outdir, dpi=300,
            poppler=[sys.executable, self.poppler])
        self.assertEqual([int(re.search(r"-(\d+)\.", os.path.basename(i)).group(1))
                          for i in images], [1, 2, 10])

    def test_missing_pdf_fails_closed(self):
        with self.assertRaises(ExtractionError):
            render_pdf_to_images(os.path.join(self.tmp.name, "nope.pdf"),
                                 poppler=[sys.executable, self.poppler])

    def test_missing_renderer_fails_closed(self):
        with self.assertRaises(ExtractionError):
            render_pdf_to_images(self.pdf, poppler=("definitely-not-a-poppler-xyz",))

    def test_dpi_bounds(self):
        with self.assertRaises(ExtractionError):
            render_pdf_to_images(self.pdf, dpi=10,
                                 poppler=[sys.executable, self.poppler])
        with self.assertRaises(ExtractionError):
            render_pdf_to_images(self.pdf, fmt="exe",
                                 poppler=[sys.executable, self.poppler])


class TesseractLanguageTests(unittest.TestCase):
    def test_invalid_language_spec_is_rejected(self):
        with self.assertRaises(ExtractionError):
            TesseractOCR("eng;rm -rf /")
        with self.assertRaises(ExtractionError):
            TesseractOCR("../../../etc/passwd")
        self.assertEqual(TesseractOCR("eng+fil").language, "eng+fil")
        self.assertEqual(TesseractOCR("fil").language, "fil")


class OcrPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outdir = os.path.join(self.tmp.name, "rendered")
        os.makedirs(self.outdir, exist_ok=True)
        self.pdf = os.path.join(self.tmp.name, "scan.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 fake source")
        self.poppler = os.path.join(self.tmp.name, "fake_pdftoppm.py")
        make_fake_pdftoppm(self.poppler)
        self.tesseract = os.path.join(self.tmp.name, "fake_tesseract.py")
        make_fake_tesseract(self.tesseract)
        self.conn = init_db(":memory:")
        self.tx = create_transaction(
            self.conn, regime="RA12009_IRR", posting_date="2026-11-05",
            mode="Competitive Bidding", award_criterion="LCRB",
            drafter_id="staff-01",
            legal_basis_note="Legal officer memo: RA 12009 applies")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _open_bids(self):
        record_bid_opening(self.conn, self.tx, "bac-chair-01", "minutes-1",
                           ["TECHNICAL_ENVELOPE"], ["bac-chair-01"])

    def _checklist(self):
        version = resolve_rules(self.conn, "2026-11-05", "RA12009_IRR")["ruleset_version"]
        return sample_checklist(version)

    def test_extract_pdf_happy_path(self):
        self._open_bids()
        verdict, result = extract_pdf(
            self.conn, self.tx, self._checklist(), self.pdf,
            StubBackend(valid_stub_response()),
            language="eng+fil",
            poppler=[sys.executable, self.poppler],
            tesseract=[sys.executable, self.tesseract],
            workdir=self.outdir, keep_workdir=True)
        self.assertEqual(verdict["decision"], "PASS")
        self.assertTrue(result.valid)
        events = [r["event_type"] for r in self.conn.execute(
            "SELECT event_type FROM ledger_event WHERE tx_id=?", (self.tx,))]
        self.assertIn("OCR_COMPLETED", events)
        self.assertIn("EXTRACTION_COMPLETED", events)

    def test_extract_pdf_invalid_model_output_abstains(self):
        self._open_bids()
        verdict, result = extract_pdf(
            self.conn, self.tx, self._checklist(), self.pdf,
            StubBackend({"documents": {}}),  # missing model_version
            poppler=[sys.executable, self.poppler],
            tesseract=[sys.executable, self.tesseract],
            workdir=self.outdir, keep_workdir=True)
        self.assertEqual(verdict["decision"], "ABSTAIN")
        self.assertFalse(result.valid)
        events = [r["event_type"] for r in self.conn.execute(
            "SELECT event_type FROM ledger_event WHERE tx_id=?", (self.tx,))]
        self.assertIn("SCHEMA_VALIDATION_FAILED", events)

    def test_extract_pdf_before_bid_opening_is_blocked(self):
        from core import PolicyBlocked
        with self.assertRaises(PolicyBlocked):
            extract_pdf(self.conn, self.tx, self._checklist(), self.pdf,
                        StubBackend(valid_stub_response()),
                        poppler=[sys.executable, self.poppler],
                        tesseract=[sys.executable, self.tesseract],
                        workdir=self.outdir, keep_workdir=True)

    def test_cli_stub_mode_prints_json(self):
        import contextlib
        import io
        from unittest import mock

        # real binaries are covered by the render/OCR tests above; here we test
        # the CLI wiring (routing, JSON, exit codes) only.
        db_path = os.path.join(self.tmp.name, "cli.db")
        file_conn = init_db(db_path)
        file_tx = create_transaction(
            file_conn, regime="RA12009_IRR", posting_date="2026-11-05",
            mode="Competitive Bidding", award_criterion="LCRB",
            drafter_id="staff-01",
            legal_basis_note="Legal officer memo: RA 12009 applies")
        version = resolve_rules(file_conn, "2026-11-05", "RA12009_IRR")["ruleset_version"]
        checklist_path = os.path.join(self.tmp.name, "checklist.json")
        with open(checklist_path, "w", encoding="utf-8") as fh:
            json.dump(sample_checklist(version), fh)
        file_conn.close()

        class _FakeResult:
            valid = True
            violations = []
            uncertain_fields = []
            model_version = "stub"
            prompt_hash = "hash"

        def fake_extract_pdf(conn, tx_id, checklist, path, backend, **_kwargs):
            return ({"decision": "PASS", "status": "PASS"}, _FakeResult())

        import extraction
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(
                extraction, "extract_pdf", side_effect=fake_extract_pdf):
            code = extraction.main([
                "ocr", "--db", db_path, "--tx-id", file_tx,
                "--checklist", checklist_path, "--file", self.pdf, "--stub",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["verdict"]["decision"], "PASS")
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["advisory_only"])


if __name__ == "__main__":
    unittest.main()