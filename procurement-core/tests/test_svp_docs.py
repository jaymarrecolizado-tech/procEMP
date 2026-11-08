"""Tests for the SVP document chain drafting (svp_documents.py, pdf_writer.py)
and the extraction schema extension (SVP document types, list field type).

Covers:
- amount_in_words (including the exact words-vs-figures error class observed
  in real sample documents)
- validate_chain fail-closed checks (words mismatch, fund-cluster drift,
  award-to-non-lowest-bid, cross references, arithmetic)
- render_pdfs refusal on invalid chains + stamped rendering when overridden
- PDF validity/determinism (stdlib writer)
- extraction.validate_output list-typed fields + new SVP document types
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import init_db
from extraction import load_schema, validate_output
from svp_documents import (
    DOC_ORDER, amount_in_words, draft_chain, render_doc_pdf, render_pdfs,
    validate_chain, _demo_params,
)


def make_chain():
    conn = init_db(":memory:", seed=False)
    docs, report = draft_chain(conn, _demo_params())
    conn.close()
    return docs, report


class AmountInWordsTests(unittest.TestCase):
    def test_basic_pesos(self):
        self.assertEqual(amount_in_words(26100),
                         "TWENTY-SIX THOUSAND ONE HUNDRED PESOS ONLY")
        # the exact (wrong) words found on a real sample PO for 26,100.00
        self.assertEqual(amount_in_words(1320),
                         "ONE THOUSAND THREE HUNDRED TWENTY PESOS ONLY")

    def test_hyphenated_compounds_and_scales(self):
        self.assertEqual(amount_in_words(450),
                         "FOUR HUNDRED FIFTY PESOS ONLY")
        self.assertEqual(amount_in_words(1_000_000), "ONE MILLION PESOS ONLY")
        self.assertEqual(amount_in_words(123_456_789),
                         "ONE HUNDRED TWENTY-THREE MILLION FOUR HUNDRED "
                         "FIFTY-SIX THOUSAND SEVEN HUNDRED EIGHTY-NINE PESOS ONLY")

    def test_centavos_and_zero(self):
        self.assertEqual(amount_in_words(26100.25),
                         "TWENTY-SIX THOUSAND ONE HUNDRED PESOS AND 25/100 "
                         "(CENTAVOS) ONLY")
        self.assertEqual(amount_in_words(0), "ZERO PESOS ONLY")

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            amount_in_words(-1)
        with self.assertRaises(ValueError):
            amount_in_words(10**13)


class ChainValidationTests(unittest.TestCase):
    def setUp(self):
        self.docs, self.report = make_chain()

    def test_clean_chain_passes(self):
        self.assertTrue(self.report["ok"], self.report["violations"])
        self.assertEqual(self.report["violations"], [])
        numbering = self.report["numbering"]
        self.assertTrue(numbering["PR"].startswith("RO2-PR-"))
        self.assertNotEqual(numbering["PR"], numbering["RFQ"])

    def test_words_vs_figures_mismatch_detected(self):
        # inject the exact manual error observed in the sample corpus
        self.docs["purchase_order"]["data"]["amount_in_words"] = \
            "ONE THOUSAND THREE HUNDRED TWENTY PESOS ONLY"  # actual: 26,100
        report = validate_chain(self.docs)
        self.assertFalse(report["ok"])
        self.assertTrue(any("amount_in_words" in v for v in report["violations"]))

    def test_fund_cluster_drift_detected(self):
        self.docs["purchase_order"]["data"]["fund_cluster"] = "Special Fund"
        report = validate_chain(self.docs)
        self.assertFalse(report["ok"])
        self.assertTrue(any("fund_cluster" in v for v in report["violations"]))

    def test_award_to_non_lowest_bidder_detected(self):
        self.docs["bac_resolution_award"]["data"]["awarded_supplier"] = \
            "SAMPLE SUPPLIER C"  # highest bid
        report = validate_chain(self.docs)
        self.assertFalse(report["ok"])
        self.assertTrue(any("lowest calculated" in v for v in report["violations"]))

    def test_broken_cross_reference_detected(self):
        self.docs["request_for_quotation"]["data"]["pr_number_ref"] = "PR-OTHER"
        report = validate_chain(self.docs)
        self.assertFalse(report["ok"])
        self.assertTrue(any("pr_number_ref" in v for v in report["violations"]))

    def test_tampered_total_detected(self):
        self.docs["purchase_request"]["data"]["total_amount"] = 1.0
        report = validate_chain(self.docs)
        self.assertFalse(report["ok"])
        self.assertTrue(any("purchase_request" in v for v in report["violations"]))

    def test_render_refuses_invalid_chain(self):
        self.docs["purchase_order"]["data"]["amount_in_words"] = "WRONG"
        with self.assertRaises(ValueError):
            render_pdfs(self.docs, os.path.join(tempfile.gettempdir(),
                                                "procemp-should-not-exist"))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="procemp-svp-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_demo_cli_renders_six_pdfs_and_manifest(self):
        out = os.path.join(self.tmp, "out")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _run_cli(["demo", "--out", out])
        self.assertEqual(rc, 0, buf.getvalue())
        files = sorted(os.listdir(out))
        self.assertIn("manifest.json", files)
        pdfs = [f for f in files if f.endswith(".pdf")]
        self.assertEqual(len(pdfs), len(DOC_ORDER))
        for f in pdfs:
            with open(os.path.join(out, f), "rb") as fh:
                data = fh.read()
            self.assertTrue(data.startswith(b"%PDF-1.4"), f)
            self.assertIn(b"%%EOF", data)
            self.assertIn(b"/Helvetica", data)
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["violations"], [])
        self.assertIn("DRAFT", manifest["status"])

    def test_render_pdfs_deterministic_bytes(self):
        docs, _ = make_chain()
        a = render_doc_pdf(docs["purchase_request"]["render"]).write_bytes()
        b = render_doc_pdf(docs["purchase_request"]["render"]).write_bytes()
        self.assertEqual(a, b)
        self.assertIn(b"AI-ASSISTED DRAFT", a)

    def test_allow_invalid_stamps_violations(self):
        docs, _ = make_chain()
        docs["purchase_order"]["data"]["fund_cluster"] = "Special Fund"
        out = os.path.join(self.tmp, "invalid")
        written, manifest = render_pdfs(docs, out, allow_invalid=True)
        self.assertEqual(len(written), len(DOC_ORDER))
        self.assertTrue(manifest["violations"])
        with open(written[0], "rb") as fh:
            self.assertIn(b"VALIDATION VIOLATIONS", fh.read())


def _run_cli(argv):
    from svp_documents import main
    return main(argv)


class ExtractionSchemaExtensionTests(unittest.TestCase):
    """The 6 SVP document types + list field type must validate cleanly and
    still reject unknown fields."""

    def setUp(self):
        self.schema = load_schema()
        self.pages = [{"page": 1, "text": "x"}]

    def _doc(self, doc_type, fields, confidence=None):
        return {"model_version": "m1", "documents": {doc_type: {
            "status": "OK", "fields": fields,
            "confidence": confidence or {k: 1.0 for k in fields},
        }}}

    def test_purchase_request_round_trip(self):
        fields = {"pr_number": "PR-2026-00001", "pr_date": "2026-11-08",
                  "items": [{"stock_no": "1", "unit": "pc",
                             "description": "Training Kits", "quantity": 50,
                             "unit_cost": 514.0, "total_cost": 25700.0}],
                  "total_amount": 27700.0, "purpose": "Sample",
                  "funds_available": True}
        result = validate_output(self._doc("purchase_request", fields),
                                 self.schema, self.pages)
        self.assertEqual(result.violations, [], result.violations)
        self.assertEqual(result.documents["purchase_request"]["items"],
                         fields["items"])

    def test_purchase_order_words_field(self):
        fields = {"po_number": "PO-2026-00001", "po_date": "2026-11-08",
                  "supplier": "SAMPLE SUPPLIER A",
                  "items": [], "total_amount": 26100.0,
                  "amount_in_words": "TWENTY-SIX THOUSAND ONE HUNDRED PESOS ONLY",
                  "fund_cluster": "Regular Fund"}
        result = validate_output(self._doc("purchase_order", fields),
                                 self.schema, self.pages)
        self.assertEqual(result.violations, [], result.violations)

    def test_unknown_svp_field_is_violation(self):
        result = validate_output(self._doc("purchase_request",
                                           {"pr_number": "X", "invented": 1}),
                                 self.schema, self.pages)
        self.assertTrue(any("hallucinated fields" in v for v in result.violations))

    def test_wrong_list_type_is_violation(self):
        result = validate_output(self._doc("purchase_request",
                                           {"items": "not-a-list"}),
                                 self.schema, self.pages)
        self.assertTrue(any("wrong type" in v for v in result.violations))


if __name__ == "__main__":
    unittest.main()
