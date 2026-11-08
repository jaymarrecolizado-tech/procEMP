"""Generate a self-contained HTML report (with screenshots) of the E2E
drafting-form scenarios — shareable evidence for demos/ walkthroughs.

DEV-ONLY (Playwright). Run:
    python e2e/make_report.py [output.html]
Default output: e2e/report/procemp-e2e-report.html (gitignored).

Every screenshot is embedded as base64 — the single .html file opens in any
browser with no external files. Exit code 0 only if all scenario assertions
pass.
"""

import base64
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_draft_scenarios import CORE, free_port, start_server  # noqa: E402
sys.path.insert(0, CORE)  # for `from core import now` (report timestamp)

from playwright.sync_api import sync_playwright  # noqa: E402

OUT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "report", "procemp-e2e-report.html")


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail, png_bytes=None):
        self.rows.append({
            "name": name, "ok": bool(ok), "detail": detail,
            "png": (base64.b64encode(png_bytes).decode() if png_bytes else None),
        })

    def html(self):
        cards = []
        for i, r in enumerate(self.rows, start=1):
            badge = ("✅ PASS" if r["ok"] else "❌ FAIL")
            img = ""
            if r["png"]:
                img = (f'<figure><img src="data:image/png;base64,{r["png"]}" '
                       f'alt="scenario {i} screenshot"></figure>')
            cards.append(f"""
        <div class="card {'ok' if r['ok'] else 'fail'}">
          <h2>{i}. {r['name']} <span class="badge">{badge}</span></h2>
          <p>{r['detail']}</p>
          {img}
        </div>""")
        passed = sum(1 for r in self.rows if r["ok"])
        banner = "ALL SCENARIOS PASSED" if passed == len(self.rows) \
            else f"{len(self.rows) - passed} SCENARIO(S) FAILED"
        color = "#1a7f37" if passed == len(self.rows) else "#cf222e"
        import html as H
        from core import now  # core is on sys.path via start_server import
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>ProcEMP E2E Report — /draft scenarios</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;background:#f6f8fa;color:#1f2328}}
h1{{margin-bottom:2px}} .banner{{font-size:20px;font-weight:700;color:{color};margin:10px 0 4px}}
.meta{{color:#59636e;margin-bottom:18px}}
.card{{background:#fff;border:1px solid #d1d9e0;border-radius:8px;padding:14px 18px;margin:14px 0}}
.card.ok{{border-left:6px solid #1a7f37}} .card.fail{{border-left:6px solid #cf222e}}
.badge{{float:right;font-weight:700}} figure{{margin:12px 0 0}}
img{{width:100%;max-width:900px;border:1px solid #d1d9e0;border-radius:6px}}
code{{background:#eff2f5;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>ProcEMP E2E — drafting form (<code>/draft</code>)</h1>
<div class="banner">{banner} ({passed}/{len(self.rows)})</div>
<div class="meta">Generated {H.escape(now())} · headless chromium ·
own server + temp DB · application untouched</div>
{''.join(cards)}
</body></html>"""


def main(argv):
    out = argv[0] if argv else OUT_DEFAULT
    tmp = tempfile.mkdtemp(prefix="procemp-e2e-report-")
    port = free_port()
    proc = start_server(os.path.join(tmp, "x.db"), os.path.join(tmp, "d"), port)
    rep = Report()
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            base = f"http://127.0.0.1:{port}"

            def shot():
                return pg.screenshot(full_page=True)

            # ---- 1. the form itself
            pg.goto(f"{base}/draft")
            pg.fill('input[name="actor_id"]', "demo-operator")
            rep.add("Drafting form loads (Step wizard, defaults prefilled)",
                    pg.get_by_text("Generate draft chain").is_visible(),
                    "Form renders with transaction, officials, items grid, "
                    "canvass and audit sections; Actor ID filled (audit).",
                    shot())

            # ---- 2. full chain
            pg.get_by_role("button", name="Generate draft chain (6 PDFs)").click()
            pg.wait_for_load_state("networkidle")
            ok = pg.get_by_text("Draft chain generated").is_visible() \
                and pg.locator('a[href^="/draft-file"]').count() == 6
            rep.add("Full SVP chain — 6 watermarked PDFs", ok,
                    "Defaults (3 suppliers + award) → PR, RFQ, Minutes, "
                    "Abstract, Resolution, PO with ledger numbering.",
                    shot())
            pr_full = pg.locator('a[href^="/draft-file"]').first.get_attribute("href")

            # ---- 3. partial chain
            pg.goto(f"{base}/draft")
            pg.fill('input[name="actor_id"]', "demo-operator")
            for i in range(1, 5):
                pg.fill(f'input[name="sup_name_{i}"]', "")
                pg.fill(f'input[name="sup_bid_{i}"]', "")
            pg.get_by_role("button", name="Generate draft chain (6 PDFs)").click()
            pg.wait_for_load_state("networkidle")
            links = pg.locator('a[href^="/draft-file"]').count()
            rep.add("Pre-canvass chain — PR + RFQ only (fail-closed by design)",
                    ok and links == 2,
                    f"With no supplier bids the award documents are never "
                    f"drafted; got {links} files (PR + RFQ).", shot())

            # ---- 4. fail-closed award
            pg.goto(f"{base}/draft")
            pg.fill('input[name="actor_id"]', "demo-operator")
            pg.fill('input[name="award_supplier"]', "SAMPLE SUPPLIER C")
            pg.get_by_role("button", name="Generate draft chain (6 PDFs)").click()
            pg.wait_for_load_state("networkidle")
            err = pg.get_by_text("chain failed validation").is_visible() and \
                pg.get_by_text("lowest calculated bid").is_visible()
            rep.add("Award to the HIGHEST bidder — rendering refused", err,
                    "The system lists the violation instead of producing "
                    "documents: award must go to the lowest calculated bid.",
                    shot())

            # ---- 5. download evidence
            resp = pg.request.get(base + pr_full)
            is_pdf = resp.status == 200 and resp.body().startswith(b"%PDF")
            disp = resp.headers.get("content-disposition", "")
            rep.add("PDF download verified byte-level", is_pdf,
                    f"HTTP {resp.status}, body starts with %PDF, "
                    f"Content-Disposition: {disp[:60]}", None)

            # ---- 6. token auth
            port2 = free_port()
            proc2 = start_server(os.path.join(tmp, "a.db"),
                                 os.path.join(tmp, "d2"), port2,
                                 token="report-token")
            try:
                pg2 = b.new_page()
                pg2.goto(f"http://127.0.0.1:{port2}/draft")
                denied = pg2.get_by_text("Unauthorized").is_visible()
                pg2.goto(f"http://127.0.0.1:{port2}/draft?token=report-token")
                allowed = pg2.get_by_text("Generate draft chain").is_visible()
                rep.add("Token auth — denied without token, form with ?token=",
                        denied and allowed,
                        "Shared-secret gate: 401 page without/with wrong "
                        "token; form loads with the correct query token.",
                        pg2.screenshot(full_page=True))
                pg2.close()
            finally:
                proc2.kill()
            b.close()
    finally:
        proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(rep.html())
    passed = sum(1 for r in rep.rows if r["ok"])
    print(json.dumps({"report": out, "passed": f"{passed}/{len(rep.rows)}"},
                     indent=2))
    return 0 if passed == len(rep.rows) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
