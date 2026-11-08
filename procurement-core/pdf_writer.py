"""Minimal stdlib-only PDF writer for ProcEMP document drafts.

Produces valid PDF 1.4 files using the PDF core fonts (Helvetica /
Helvetica-Bold, WinAnsi encoding) — no third-party packages, matching the
project's stdlib-only constraint. Supports the layout primitives the
procurement forms need: positioned text, lines, rectangles, and simple
tables. Output is deterministic (no timestamps) so tests can compare bytes
and drafts can be diffed.

Text is encoded to cp1252 (WinAnsi); characters outside it (e.g. the peso
sign U+20B1) are transliterated (P -> "PHP") rather than dropped, and the
replacement is visible so nothing is silently lost.

This renders DRAFT documents. A rendered PDF is still an AI-assisted draft
until named humans approve/sign; it is never an official GPPB form body
(release controls live in forms.py).

  from pdf_writer import PDF
  pdf = PDF()
  page = pdf.add_page()
  page.text(72, 100, 12, "PURCHASE REQUEST", bold=True, center=468)
  pdf.write_bytes() -> bytes
"""

from __future__ import annotations

import io

PAGE_WIDTH = 612   # US Letter, points
PAGE_HEIGHT = 792


def _cp1252(text):
    """Encode to WinAnsi; transliterate common PHP-document characters that
    WinAnsi lacks instead of failing or dropping them."""
    replacements = {"\u20b1": "PHP ", "\u2013": "-", "\u2014": "-",
                    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                    "\u2026": "...", "\u00d7": "x"}
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text.encode("cp1252", errors="replace").decode("cp1252")


def _escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_width(text, size, bold=False):
    """Approximate Helvetica width for centering/right-align (avg 0.55em)."""
    return len(text) * size * (0.58 if bold else 0.52)


class PDFPage:
    def __init__(self, pdf):
        self.pdf = pdf
        self.ops = []

    # y is measured from the TOP of the page (converted internally)
    def text(self, x, y, size, s, bold=False, center=None, right=None):
        if center is not None:
            x = center - _text_width(s, size, bold) / 2
        elif right is not None:
            x = right - _text_width(s, size, bold)
        font = "/F2" if bold else "/F1"
        self.ops.append(
            f"BT {font} {size} Tf {x:.2f} {PAGE_HEIGHT - y:.2f} Td "
            f"({_escape(_cp1252(s))}) Tj ET")

    def line(self, x1, y1, x2, y2, width=0.7):
        self.ops.append(
            f"{width} w {x1:.2f} {PAGE_HEIGHT - y1:.2f} m "
            f"{x2:.2f} {PAGE_HEIGHT - y2:.2f} l S")

    def rect(self, x, y, w, h, width=0.7):
        self.ops.append(
            f"{width} w {x:.2f} {PAGE_HEIGHT - y - h:.2f} {w:.2f} {h:.2f} re S")


class PDF:
    """A deterministic PDF document. Pages hold top-down coordinates."""

    def __init__(self):
        self.pages = []

    def add_page(self):
        page = PDFPage(self)
        self.pages.append(page)
        return page

    def write_bytes(self):
        objects = []  # 1-indexed object bodies (bytes), assembled below

        def add(body):
            objects.append(body)
            return len(objects)

        # reserve: 1 catalog, 2 pages tree; then fonts, page/content pairs
        catalog_id = add(None)
        pages_id = add(None)
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                 b"/Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                 b"/Encoding /WinAnsiEncoding >>")
        page_ids, content_ids = [], []
        for page in self.pages:
            stream = "\n".join(page.ops).encode("cp1252", errors="replace")
            content_ids.append(add(
                b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                + stream + b"\nendstream"))
            page_ids.append(add(None))
        kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
        objects[catalog_id - 1] = (
            b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>")
        objects[pages_id - 1] = (
            b"<< /Type /Pages /Kids [" + kids + b"] /Count "
            + str(len(page_ids)).encode() + b" >>")
        for pid, cid in zip(page_ids, content_ids):
            objects[pid - 1] = (
                b"<< /Type /Page /Parent " + str(pages_id).encode()
                + b" 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 "
                + str(f1).encode() + b" 0 R /F2 " + str(f2).encode()
                + b" 0 R >> >> /Contents " + str(cid).encode() + b" 0 R >>")

        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, body in enumerate(objects, start=1):
            offsets.append(out.tell())
            out.write(f"{i} 0 obj\n".encode())
            out.write(body if body is not None else b"")
            out.write(b"\nendobj\n")
        xref_at = out.tell()
        out.write(f"xref\n0 {len(objects) + 1}\n".encode())
        out.write(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            out.write(f"{off:010d} 00000 n \n".encode())
        out.write(
            b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root " + str(catalog_id).encode() + b" 0 R >>\nstartxref\n"
            + str(xref_at).encode() + b"\n%%EOF\n")
        return out.getvalue()

    def write_file(self, path):
        data = self.write_bytes()
        with open(path, "wb") as fh:
            fh.write(data)
        return path
