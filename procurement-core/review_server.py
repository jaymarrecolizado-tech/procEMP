"""Loopback web UI for the ProcEMP advisory BAC review queue.

This is a decision-support interface, not a decision-making system.
It helps reviewers inspect AI-assisted verdicts (evidence, citations,
confidence, missing/invalid items) and record advisory actions. Statutory
procurement determinations must still be made and signed by authorized human
bodies through the governed workflow/gates.

Writes (claim/recommend/resolve/override) are audited via review_queue/core
ledger events. Bind to loopback only by default.

Hardening (Track C):
- Optional shared-secret auth: --auth-token REQ_TOKEN / REVIEW_AUTH_TOKEN env.
  When set, every route except /health requires X-Auth-Token header (checked
  with hmac.compare_digest). For production, put an authenticated reverse
  proxy in front; this is defense-in-depth for a loopback service.
- RBAC: action/role checks are enforced in review_queue.require_role().
- /export is disabled over HTTP (arbitrary file write risk); use the CLI
  `python review_queue.py export --out <path>` instead.
- Optional TLS (ops_tls.py): --tls-cert/--tls-key, optional --tls-client-ca
  for mutual TLS. Defensive headers (CSP, no-sniff, frame-deny, HSTS on TLS)
  are sent on every response. Transport protection never replaces human gates.
"""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import CoreError, _connect, append_event, canonical, now
from ops_tls import (
    add_security_headers, add_tls_arguments, build_tls_context, scheme_for,
    wrap_server,
)
from rag import AsOfRAG
from review_queue import (
    ADJUDICATE_ROLES, ADVISORY_RECOMMENDATIONS, ROLE_PERMISSIONS,
    adjudicate_override, claim_item, export_queue_dashboard,
    get_item, list_queue, queue_dashboard, recommend, resolve_item,
    submit_override_correction,
)
from svp_documents import _demo_params, draft_chain, render_pdfs

# In-memory registry of drafts generated this process, keyed by PR number.
# Downloads are served only from this registry — never from user-supplied
# paths — which makes path traversal impossible by construction.
DRAFT_RESULTS = {}


def _assert_loopback(host):
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise CoreError(f"refusing to bind review UI to non-loopback host: {host}")


def esc(text):
    return html.escape("" if text is None else str(text), quote=True)


def _token_field(auth_token):
    if not auth_token:
        return ""
    return f'<input type="hidden" name="auth_token" value="{esc(auth_token)}">'


def page(title, body, message=None, error=None, active=""):
    flash = ""
    if message:
        flash += f'<div class="flash ok" role="status">{esc(message)}</div>'
    if error:
        flash += f'<div class="flash bad" role="alert">{esc(error)}</div>'
    q = ' class="nav-link active"' if active == "queue" else ' class="nav-link"'
    d = ' class="nav-link active"' if active == "draft" else ' class="nav-link"'
    generated = esc(now().replace("T", " · "))
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root{{
  --bg:#e9edf4; --panel:#ffffff; --ink:#111e2c; --muted:#55616f; --line:#d9e0ea;
  --brand:#0c3d5d; --brand-2:#144e78; --brand-dark:#082a42; --accent:#d8b545; --accent-dark:#a8861e;
  --ok:#176e46; --ok-bg:#edf6ef; --ok-line:#bfe3cd;
  --bad:#a63a3a; --bad-bg:#fdf0ee; --bad-line:#f2c6c1;
  --warn:#7a5200; --warn-bg:#fff6df; --warn-line:#e8d194;
  --info:#1f4f7a; --info-bg:#edf3fb; --info-line:#c4d7ee;
  --shadow:0 1px 2px rgba(17,30,44,.06), 0 10px 28px rgba(17,30,44,.07);
  --radius:12px;
}}
*{{box-sizing:border-box}}
html{{scrollbar-color:var(--brand-2) transparent}}
body{{margin:0;font:15px/1.55 "Segoe UI",system-ui,-apple-system,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}}
.skip-link{{position:absolute;left:-9999px;top:0;padding:10px 16px;background:var(--brand);color:#fff;border-radius:0 0 8px 0;z-index:30;text-decoration:none}}
.skip-link:focus{{left:0;top:0}}
:focus-visible{{outline:3px solid var(--accent);outline-offset:2px;border-radius:2px}}
a{{color:var(--brand-2);text-decoration-thickness:1.5px;text-underline-offset:2px}}
a:hover{{color:var(--brand-dark)}}
.mono, code{{font-family:Consolas,SFMono-Regular,Menlo,monospace;font-size:.92em;background:#eef1f6;border:1px solid var(--line);border-radius:6px;padding:1px 6px;white-space:nowrap}}
.muted{{color:var(--muted)}}
.small{{font-size:12.5px}}

/* ---------- top bar ---------- */
.topbar{{background:linear-gradient(180deg,#0a2c44 0%, var(--brand-dark) 100%);color:#fff;border-bottom:3px solid var(--accent)}}
.topbar-inner{{max-width:1320px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
.brand-text{{display:flex;flex-direction:column;line-height:1.15}}
.brand-name{{font-size:22px;font-weight:800;letter-spacing:.2px;color:#fff}}
.brand-tag{{font-size:10.5px;color:#c2d8e9;letter-spacing:1.6px;text-transform:uppercase}}
.topbar-right{{margin-left:auto;display:flex;align-items:center;gap:14px;font-size:11.5px;color:#aecbdE}}
.pill-static{{display:inline-flex;align-items:center;gap:7px;background:var(--accent);color:#2b2306;font-weight:800;letter-spacing:1.2px;font-size:10.5px;border-radius:999px;padding:5px 12px}}
.pill-static::before{{content:"";width:7px;height:7px;border-radius:50%;background:#2b2306}}
.topbar-meta{{text-align:right;color:#b9d0e0}}
.topbar-meta time{{display:block;color:#e7eff6;font-size:12px}}
.subbar{{background:var(--brand);border-bottom:1px solid #06243a}}
.subbar-inner{{max-width:1320px;margin:0 auto;padding:0 18px;display:flex;gap:2px;flex-wrap:wrap}}
.nav-link{{display:inline-flex;align-items:center;gap:8px;color:#d3e3ee;text-decoration:none;
           padding:11px 16px;font-weight:600;font-size:13px;border-bottom:3px solid transparent;transition:background .15s ease,color .15s ease}}
.nav-link:hover{{background:rgba(255,255,255,.08);color:#fff}}
.nav-link.active{{color:#fff;border-bottom-color:var(--accent);background:rgba(255,255,255,.07)}}

/* ---------- notice ---------- */
.advisory{{display:flex;gap:12px;align-items:flex-start;margin:18px 0 20px;padding:12px 16px;
          background:var(--warn-bg);border:1px solid var(--warn-line);border-left:5px solid var(--accent);
          border-radius:10px;color:var(--warn);font-size:13px}}
.advisory strong{{color:#5c3d00}}
.advisory .mark{{flex:0 0 auto;width:22px;height:22px;border-radius:50%;background:var(--accent);color:#2b2306;
                 display:inline-flex;align-items:center;justify-content:center;font-weight:800;font-size:13px}}

/* ---------- generic ---------- */
main{{max-width:1320px;margin:0 auto;padding:0 24px 48px}}
h1,h2,h3{{margin:0}}
.page-head{{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;margin:6px 0 16px}}
.page-head h1{{font-size:26px;letter-spacing:-.01em}}
.page-head p{{margin:4px 0 0;color:var(--muted);font-size:13.5px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}}
.card-pad{{padding:20px}}
.card-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line)}}
.card-head h2{{font-size:15px;letter-spacing:.02em;color:var(--brand-dark);text-transform:uppercase}}

/* stats */
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin:0 0 18px}}
.stat{{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:14px 16px 12px;border-top:4px solid var(--brand-2)}}
.stat .label{{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted)}}
.stat .num{{font-size:30px;font-weight:800;margin-top:2px;text-align:left}}
.stat .cap{{font-size:11px;color:var(--muted);margin-top:1px}}
.stat-open{{border-top-color:var(--accent)}}
.stat-claimed{{border-top-color:var(--info)}}
.stat-escalated{{border-top-color:var(--bad)}}
.stat-resolved{{border-top-color:var(--ok)}}

/* segmented filter */
.filters{{display:inline-flex;background:#fff;border:1px solid var(--line);border-radius:10px;padding:4px;gap:2px;box-shadow:var(--shadow)}}
.filters a{{padding:7px 13px;border-radius:8px;font-size:12.5px;font-weight:600;color:var(--muted);text-decoration:none}}
.filters a:hover{{background:var(--info-bg);color:var(--info)}}
.filters a.on{{background:var(--brand);color:#fff}}

/* tables */
.table-wrap{{overflow-x:auto;max-width:100%}}
table{{width:100%;max-width:100%;border-collapse:collapse;font-size:13px;table-layout:auto}}
tbody td{{word-break:break-word;overflow-wrap:anywhere}}
thead th{{text-align:left;font-size:10.5px;letter-spacing:1.1px;text-transform:uppercase;color:#fff;background:var(--brand-2);padding:10px 14px;white-space:nowrap}}
tbody td{{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:middle}}
tbody tr:nth-child(even){{background:#f7fafd}}
tbody tr:hover{{background:#eef4fb}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:0}}
.empty{{padding:42px 16px;text-align:center;color:var(--muted)}}
.count-chip{{background:var(--info-bg);border:1px solid var(--info-line);color:var(--info);border-radius:999px;padding:3px 12px;font-size:12px;font-weight:700}}

/* badges */
.badge{{display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:999px;font-size:11.5px;font-weight:700;border:1px solid transparent;white-space:nowrap}}
.badge::before{{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}}
.open,.high,.critical{{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad)}}
.fail,.abstain{{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad)}}
.pass,.resolved{{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok)}}
.claimed{{background:var(--info-bg);border-color:var(--info-line);color:var(--info)}}
.escalated{{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn)}}
.low,.normal{{background:#eef1f6;border-color:var(--line);color:var(--muted)}}

/* definition lists */
.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px 22px;padding:18px 20px}}
.meta .k{{font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted)}}
.meta .v{{font-size:14px;margin-top:3px}}
.dl-grid{{display:grid;grid-template-columns:auto 1fr;gap:6px 16px}}
.dl-grid dt{{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding-top:2px}}
.dl-grid dd{{margin:0}}

/* item page */
.crumb{{font-size:12.5px;margin:10px 0 2px;color:var(--muted)}}
.item-title{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}}
.item-title h1{{font-size:24px;letter-spacing:-.01em}}
.item-layout{{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:18px;align-items:start}}
@media (max-width:1100px){{.item-layout{{grid-template-columns:1fr}}}}
.stack{{display:flex;flex-direction:column;gap:16px}}
.section-gap{{margin-top:14px}}
.sidebar{{position:sticky;top:18px;display:flex;flex-direction:column;gap:14px}}
@media (max-width:1100px){{.sidebar{{position:static}}}}
.side-title{{font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:#fff;background:var(--brand-2);padding:10px 16px;border-radius:var(--radius) var(--radius) 0 0}}
.action-card{{background:var(--panel);border:1px solid var(--line);border-top:0;padding:14px 16px}}
.action-card:last-child{{border-radius:0 0 var(--radius) var(--radius)}}
.action-card h3{{font-size:13px;display:flex;align-items:center;gap:8px;color:var(--brand-dark);margin-bottom:2px}}
.step{{display:inline-flex;width:22px;height:22px;border-radius:50%;background:var(--brand);color:#fff;font-size:11px;align-items:center;justify-content:center;font-weight:800;flex:0 0 auto}}
.hint{{font-size:11px;color:var(--muted);margin-top:8px}}
.divider{{border:0;border-top:1px solid var(--line);margin:10px 0}}

/* forms */
fieldset.card, form.card{{margin:0;border:1px solid var(--line);border-radius:var(--radius);padding:0;background:var(--panel);box-shadow:var(--shadow)}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:16px 18px;margin:0}}
legend, .panel-legend{{font-size:12px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:var(--brand-dark);background:var(--info-bg);border:1px solid var(--info-line);padding:6px 12px;border-radius:8px}}
label{{display:block;font-size:11px;font-weight:700;letter-spacing:.3px;color:var(--muted);margin-top:12px}}
label:first-child{{margin-top:0}}
input,select,textarea{{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fbfcfe}}
input:focus,select:focus,textarea:focus{{border-color:var(--brand-2);background:#fff;box-shadow:0 0 0 3px rgba(20,78,120,.15);outline:none}}
textarea{{min-height:76px;resize:vertical}}
.btn{{display:block;width:100%;margin-top:12px;padding:12px 14px;border:0;border-radius:9px;background:var(--brand);color:#fff;font-weight:800;letter-spacing:.2px;cursor:pointer;transition:background .15s ease}}
.btn:hover{{background:var(--brand-2)}}
.btn:focus-visible{{outline:3px solid var(--accent);outline-offset:2px}}
.btn-accent{{background:var(--accent);color:#2b2306}}
.btn-accent:hover{{background:var(--accent-dark);color:#fff}}
.btn-quiet{{width:auto;display:inline-flex}}

/* drafting form */
.two-col{{display:grid;grid-template-columns:1.15fr .85fr;gap:16px}}
@media (max-width:1000px){{.two-col{{grid-template-columns:1fr}}}}
.step-card{{border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);box-shadow:var(--shadow);padding:16px 18px;margin:0 0 16px}}
.step-card legend, .panel-legend{{display:inline-block;font-size:11px;font-weight:800;letter-spacing:1.4px;text-transform:uppercase;color:var(--brand-dark);background:var(--info-bg);border:1px solid var(--info-line);padding:6px 14px;border-radius:8px;margin-bottom:4px}}
.sup-row{{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:10px 0;border-bottom:1px dashed var(--line)}}
.sup-row:last-child{{border-bottom:0}}
.chk{{display:flex !important;align-items:center;gap:10px;font-size:13px !important;margin-top:14px !important;color:var(--muted) !important;font-weight:600 !important}}
.chk input{{width:auto !important}}
.error{{color:var(--bad);font-weight:700}}
.draft-items td,.draft-items th{{font-size:12.5px}}
.draft-items input,.draft-items textarea{{padding:6px 8px;font-size:12.5px}}
.draft-items textarea{{min-height:52px}}

/* details */
details{{margin-top:4px}}
summary{{cursor:pointer;color:var(--brand-2);font-weight:600;font-size:13px}}
summary:hover{{color:var(--brand-dark)}}
pre{{white-space:pre-wrap;word-break:break-word;background:#f4f6fa;border:1px solid var(--line);padding:10px 12px;border-radius:8px;font-size:12px}}

/* flash */
.flash{{padding:11px 14px 11px 42px;border-radius:10px;margin:14px 0;position:relative;border:1px solid transparent;font-size:13.5px}}
.flash::before{{content:"i";position:absolute;left:12px;top:50%;transform:translateY(-50%);width:20px;height:20px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px}}
.flash.ok{{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok)}}
.flash.ok::before{{content:"\u2713";background:var(--ok);color:#fff}}
.flash.bad{{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad)}}
.flash.bad::before{{content:"!";background:var(--bad);color:#fff}}

/* footer */
.site-footer{{border-top:1px solid var(--line);margin-top:8px}}
.site-footer-inner{{max-width:1320px;margin:0 auto;padding:16px 24px 22px;display:flex;gap:24px;flex-wrap:wrap;justify-content:space-between;font-size:11.5px;color:var(--muted)}}
.site-footer strong{{color:var(--brand-dark)}}

@media (max-width:900px){{
  .col-optional{{display:none}}
  thead th{{padding:9px 10px;font-size:10px}}
  tbody td{{padding:9px 10px}}
}}
@media (max-width:860px){{
  .meta{{grid-template-columns:1fr 1fr;gap:12px}}
  .topbar-right{{margin-left:0;width:100%;justify-content:space-between}}
  .page-head h1{{font-size:22px}}
}}
@media (max-width:560px){{.meta{{grid-template-columns:1fr}}.filters a{{padding:7px 9px;font-size:11.5px}}}}
@media (prefers-reduced-motion:reduce){{*{{transition:none !important}}}}
</style></head><body>
<a class="skip-link" href="#main">Skip to main content</a>
<header class="topbar">
  <div class="topbar-inner">
    <svg width="46" height="46" viewBox="0 0 48 48" role="img" aria-label="ProcEMP logo" aria-hidden="true">
      <rect x="1" y="1" width="46" height="46" rx="10" fill="#d8b545"/>
      <path d="M17 8h15a2.5 2.5 0 0 1 2.5 2.5V38a2.5 2.5 0 0 1-2.5 2.5H17A2.5 2.5 0 0 1 14.5 38V10.5A2.5 2.5 0 0 1 17 8z" fill="#0c3d5d"/>
      <path d="M20.5 16.5h8M20.5 21.5h8" stroke="#d8b545" stroke-width="1.8" stroke-linecap="round"/>
      <circle cx="24.5" cy="30.5" r="3.6" fill="none" stroke="#d8b545" stroke-width="1.8"/>
      <path d="M23 30.6l1.2 1.2 2.5-2.5" stroke="#d8b545" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <div class="brand-text">
      <span class="brand-name">ProcEMP</span>
      <span class="brand-tag">AI-Assisted Procurement Workflow</span>
    </div>
    <div class="topbar-right">
      <span class="pill-static">ADVISORY ONLY</span>
      <span class="topbar-meta">Generated<time>{generated}</time></span>
    </div>
  </div>
  <nav class="subbar" aria-label="Primary navigation">
    <div class="subbar-inner">
      <a{q} href="/">Review queue</a>
      <a{d} href="/draft">SVP drafting</a>
      <a class="nav-link" href="/health">Health (JSON)</a>
    </div>
  </nav>
</header>
<main id="main">
  <div class="advisory" role="note">
    <span class="mark" aria-hidden="true">i</span>
    <span><strong>Advisory decision support.</strong> AI flags and suggestions do <strong>not</strong> make statutory procurement decisions.
    Authorized humans (BAC/TWG/HOPE/Inspection/Accounting as applicable) must review the evidence and record/sign official determinations through the governed workflow.</span>
  </div>
  {flash}{body}
</main>
<footer class="site-footer">
  <div class="site-footer-inner">
    <span><strong>ProcEMP</strong> prototype · dual regime RA 12009 / RA 9184 (transitory)</span>
    <span>Loopback-only prototype · not for official determinations · named human bodies decide and sign</span>
  </div>
</footer>
</body></html>"""


def render_index(conn, params, message=None, error=None, auth_token=None):
    status = params.get("status") or None
    if status not in (None, "OPEN", "CLAIMED", "ESCALATED", "RESOLVED"):
        status = None
    dash = queue_dashboard(conn)
    items = list_queue(conn, status=status, limit=200)["items"]

    def link(label, value):
        on = " on" if status == value or (value is None and status is None) else ""
        href = "/" if value is None else f"/?status={value}"
        return f"<a class='{on.strip()}' href='{href}'>{label}</a>"

    seg = f"<div class='filters' role='group' aria-label='Filter queue by status'>" + "".join([
        link("All", None),
        link("Open", "OPEN"),
        link("Claimed", "CLAIMED"),
        link("Escalated", "ESCALATED"),
        link("Resolved", "RESOLVED"),
    ]) + "</div>"

    rows = []
    for it in items:
        rows.append(
            "<tr>"
            f"<td><a class='mono' href='/item?id={esc(it['id'])}'>{esc(it['id'][:8])}</a></td>"
            f"<td class='mono'>{esc(it.get('tx_number') or it.get('tx_id'))}</td>"
            f"<td><span class='badge {esc((it.get('decision') or '').lower())}'>{esc(it.get('decision'))}</span></td>"
            f"<td class='num'>{esc(it.get('confidence'))}</td>"
            f"<td><span class='badge {esc(it['status'].lower())}'>{esc(it['status'])}</span>&nbsp;<span class='badge low'>{esc(it['reason'])}</span></td>"
            f"<td class='col-optional'><span class='badge {esc((it.get('priority') or '').lower())}'>{esc(it.get('priority'))}</span></td>"
            f"<td class='small'>{esc(it.get('missing_documents'))}</td><td class='small col-optional'>{esc(it.get('invalid_documents'))}</td>"
            f"<td class='col-optional'>{esc(it.get('assigned_to') or '—')}</td>"
            "</tr>"
        )
    table = (
        "<div class='table-wrap'><table><thead><tr><th>Item</th><th>Tx</th><th>Decision</th>"
        "<th class='num'>Conf.</th><th>Status / Reason</th><th class='col-optional'>Priority</th>"
        "<th>Missing</th><th class='col-optional'>Invalid</th><th class='col-optional'>Assigned</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=9 class=\"empty\">No items match this filter.</td></tr>'}</tbody></table></div>"
    )
    body = f"""
    <div class="page-head">
      <div>
        <h1>Review queue</h1>
        <p>AI-flagged verdicts awaiting human attention — advisory only, never a statutory determination.</p>
      </div>
      {seg}
    </div>

    <div class="stat-grid" aria-label="Queue summary">
      <div class="stat stat-open"><div class="label">Open</div><div class="num">{dash['open_items']}</div><div class="cap">Awaiting human review</div></div>
      <div class="stat stat-claimed"><div class="label">Claimed</div><div class="num">{dash['claimed_items']}</div><div class="cap">Being reviewed now</div></div>
      <div class="stat stat-escalated"><div class="label">Escalated</div><div class="num">{dash['escalated_items']}</div><div class="cap">Requires higher-level review</div></div>
      <div class="stat stat-resolved"><div class="label">Resolved</div><div class="num">{dash['resolved_items']}</div><div class="cap">Review complete (advisory)</div></div>
    </div>

    <section class="card">
      <div class="card-head"><h2>Items</h2><span class="count-chip">{len(items)} shown</span></div>
      {table}
    </section>
    """
    return page("ProcEMP Advisory Review Queue", body, message, error, active="queue")


ALL_ROLES = tuple(sorted(
    {role for roles in ROLE_PERMISSIONS.values() for role in roles}
    | set(ADJUDICATE_ROLES)
))


def _allowed_hint(action):
    return "Allowed: " + ", ".join(ROLE_PERMISSIONS.get(action, ()))


def _role_datalist():
    options = "".join(f'<option value="{esc(role)}"></option>' for role in ALL_ROLES)
    return f'<datalist id="roles">{options}</datalist>'


def _documents_table(conn, tx_id):
    rows = conn.execute(
        "SELECT id, artifact_class, content_hash, ingested_at, opening_seq "
        "FROM document WHERE tx_id=? ORDER BY ingested_at", (tx_id,)
    ).fetchall()
    if not rows:
        return '<p class="muted">No documents ingested for this transaction.</p>'
    rows_html = "".join(
        f"<tr><td>{esc(r['id'][:8])}</td><td>{esc(r['artifact_class'])}</td>"
        f"<td><code>{esc((r['content_hash'] or '')[:16])}…</code></td>"
        f"<td>{esc(r['ingested_at'])}</td><td>{esc(r['opening_seq'])}</td></tr>"
        for r in rows
    )
    return (
        "<table><thead><tr><th>Document</th><th>Artifact class</th>"
        "<th>Content hash</th><th>Ingested at</th><th>Opening seq</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        "<p class=muted>Metadata only — bid content is never stored by this system; "
        "sealed-bid custody is enforced at ingest (IRR Sec. 58).</p>"
    )


def _citations_table(conn, tx_id, citations):
    if not citations:
        return '<p class="muted">No rule citations on this verdict.</p>'
    rag = AsOfRAG(conn)
    rows_html = []
    for c in citations:
        c = c if isinstance(c, dict) else {}
        chunk_id = c.get("chunk_id")
        passage = ""
        status = "—"
        if chunk_id:
            row = conn.execute(
                "SELECT text, passage_hash FROM corpus_chunk WHERE chunk_id=?",
                (chunk_id,),
            ).fetchone()
            if row:
                text = row["text"] or ""
                passage = (text[:300] + ("…" if len(text) > 300 else ""))
            ok, reason = rag.verify(c, tx_id)
            status = ('<span class="badge pass">verified</span>' if ok
                      else f'<span class="badge fail">FAILED: {esc(reason)}</span>')
        else:
            status = '<span class="badge low">unverified — no chunk_id</span>'
        rows_html.append(
            f"<tr><td>{esc(c.get('section'))}</td>"
            f"<td>{esc(c.get('chunk_id') or '—')}</td>"
            f"<td>{esc(passage or '—')}</td><td>{status}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Cited section</th><th>Chunk</th>"
        "<th>Passage (excerpt)</th><th>Verification</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table>"
    )


def _events_table(conn, tx_id, limit=50):
    rows = conn.execute(
        "SELECT seq, event_type, actor_id, actor_role, occurred_at "
        "FROM ledger_event WHERE tx_id=? ORDER BY seq DESC LIMIT ?",
        (tx_id, limit),
    ).fetchall()
    if not rows:
        return '<p class="muted">No ledger events for this transaction.</p>'
    rows_html = "".join(
        f"<tr><td>{esc(r['seq'])}</td><td>{esc(r['event_type'])}</td>"
        f"<td>{esc(r['actor_id'])}</td><td>{esc(r['actor_role'])}</td>"
        f"<td>{esc(r['occurred_at'])}</td></tr>"
        for r in reversed(rows)
    )
    return (
        "<table><thead><tr><th>Seq</th><th>Event</th><th>Actor</th>"
        "<th>Role</th><th>At</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
    )


def render_item(conn, item_id, message=None, error=None, auth_token=None):
    it = get_item(conn, item_id)
    v = it.get("verdict") or {}
    actions_items = []
    for a in it.get("actions") or []:
        item = (f"<li><span class='mono'>{esc(a['created_at'])}</span> — <strong>{esc(a['action'])}</strong>"
                f" by {esc(a['actor_id'])} ({esc(a['actor_role'])})")
        if a.get('note'):
            item += f"<div class='small muted'>{esc(a['note'])}</div>"
        item += f"<pre>{esc(canonical(a.get('payload')))}</pre></li>"
        actions_items.append(item)
    actions_html = "".join(actions_items) or "<li class='muted'>No actions yet.</li>"
    tx_number = esc(it.get('tx_number') or it.get('tx_id'))
    body = f"""
    <div class="crumb"><a href="/">Review queue</a> <span aria-hidden="true">›</span> Item <span class="mono">{esc(it['id'][:8])}</span></div>
    <div class="item-title">
      <h1>{tx_number}</h1>
      <span class="badge {esc((v.get('decision') or '').lower())}">{esc(v.get('decision') or '—')} (advisory)</span>
      <span class="badge {esc(it['status'].lower())}">{esc(it['status'])}</span>
      <span class="badge {esc((it.get('priority') or '').lower())}">Priority {esc(it.get('priority'))}</span>
      <span class="badge low">{esc(it['reason'])}</span>
    </div>

    <div class="card meta section-gap">
      <div><div class="k">Transaction</div><div class="v mono">{tx_number}</div></div>
      <div><div class="k">Mode / Stage</div><div class="v">{esc(it.get('tx_mode'))} / {esc(it.get('tx_stage'))}</div></div>
      <div><div class="k">Regime / Ruleset</div><div class="v">{esc(it.get('tx_regime'))} / <span class="mono">{esc(v.get('ruleset_version'))}</span></div></div>
      <div><div class="k">Corpus snapshot</div><div class="v mono">{esc(v.get('corpus_snapshot_id'))}</div></div>
      <div><div class="k">Model version</div><div class="v mono">{esc(v.get('model_version'))}</div></div>
      <div><div class="k">Confidence</div><div class="v">{esc(v.get('confidence'))}</div></div>
      <div><div class="k">Assigned to</div><div class="v">{esc(it.get('assigned_to') or '—')}</div></div>
      <div><div class="k">Verdict</div><div class="v mono">{esc(it['verdict_id'][:16])}…</div></div>
    </div>

    <div class="advisory section-gap" role="note">
      <span class="mark" aria-hidden="true">i</span>
      <span><strong>Suggested next step (advisory):</strong> Verify the evidence and source documents, then record an advisory action.
      Any statutory determination requires the authorized human body and governed sign-off.</span>
    </div>

    <div class="item-layout section-gap">
      <div class="stack">
        <section class="card">
          <div class="card-head"><h2>Provenance (advisory lineage)</h2></div>
          <div class="card-pad">
            <dl class="dl-grid">
              <dt>Prompt hash</dt><dd class="mono">{esc(v.get('prompt_hash'))}</dd>
              <dt>Rules engine</dt><dd>{esc(v.get('rules_engine_version'))}</dd>
              <dt>Missing</dt><dd>{esc(v.get('missing_documents'))}</dd>
              <dt>Invalid</dt><dd>{esc(v.get('invalid_documents'))}</dd>
              <dt>Item ID</dt><dd class="mono">{esc(it['id'])}</dd>
            </dl>
          </div>
        </section>

        <section class="card">
          <div class="card-head"><h2>Evidence spans</h2></div>
          <div class="table-wrap"><table><thead><tr><th>Field</th><th>Value</th><th class="num">Page</th><th>Snippet</th></tr></thead><tbody>
          {''.join(f"<tr><td><span class='mono'>{esc(s.get('field'))}</span></td><td>{esc(s.get('value'))}</td><td class='num'>{esc(s.get('page',''))}</td><td>{esc(s.get('snippet',''))}</td></tr>" for s in (v.get('evidence_spans') or []) if isinstance(s, dict)) or '<tr><td colspan=4 class="empty">No evidence spans on this verdict.</td></tr>'}
          </tbody></table></div>
        </section>

        <details class="card card-pad"><summary>Notes</summary><pre>{esc(canonical(v.get('notes', [])))}</pre></details>

        <section class="card">
          <div class="card-head"><h2>Rule citations (passage + verification)</h2></div>
          <div class="table-wrap">{_citations_table(conn, it["tx_id"], v.get("rule_citations", []))}</div>
        </section>
        <details class="card card-pad"><summary>Rule citations (raw JSON)</summary><pre>{esc(canonical(v.get('rule_citations', [])))}</pre></details>

        <section class="card">
          <div class="card-head"><h2>Ingested documents (metadata only, sealed-bid custody enforced)</h2></div>
          <div class="table-wrap">{_documents_table(conn, it["tx_id"])}</div>
        </section>

        <section class="card">
          <div class="card-head"><h2>Ledger timeline (advisory audit trail)</h2></div>
          <div class="table-wrap">{_events_table(conn, it["tx_id"])}</div>
        </section>

        <details class="card card-pad"><summary>Full verdict payload</summary><pre>{esc(canonical(v))}</pre></details>

        <section class="card">
          <div class="card-head"><h2>Recorded actions</h2></div>
          <ul class="card-pad" style="list-style:none;margin:0">{actions_html}</ul>
        </section>
      </div>

      <aside class="sidebar" aria-label="Advisory actions">
        <div class="side-title">Advisory actions — named humans only</div>
        <form class="action-card" method="post" action="/action">{_token_field(auth_token)}
          <h3><span class="step">1</span>Claim</h3><input type="hidden" name="action" value="claim"><input type="hidden" name="item_id" value="{esc(item_id)}">
          <label>Actor ID (named human)</label><input name="actor_id" required placeholder="e.g. bac-member-01">
          <label>Role</label><input name="actor_role" value="BAC_MEMBER" list="roles">
          <label>Note</label><textarea name="note" placeholder="Optional review note"></textarea><button class="btn" type="submit">Claim item</button>
          <p class="hint">{esc(_allowed_hint("CLAIM"))}</p>
        </form>
        <form class="action-card" method="post" action="/action">{_token_field(auth_token)}
          <h3><span class="step">2</span>Advisory recommendation</h3><input type="hidden" name="action" value="recommend"><input type="hidden" name="item_id" value="{esc(item_id)}">
          <label>Actor ID</label><input name="actor_id" required placeholder="e.g. bac-member-01">
          <label>Role</label><input name="actor_role" value="BAC_MEMBER" list="roles">
          <label>Recommendation</label><select name="recommendation">{"".join(f'<option>{r}</option>' for r in sorted(ADVISORY_RECOMMENDATIONS))}</select>
          <label>Note</label><textarea name="note"></textarea><button class="btn" type="submit">Record recommendation</button>
          <p class="hint">{esc(_allowed_hint("RECOMMEND_REVIEW"))} (per recommendation)</p>
        </form>
        <form class="action-card" method="post" action="/action">{_token_field(auth_token)}
          <h3><span class="step">3</span>Resolve (advisory workflow)</h3><input type="hidden" name="action" value="resolve"><input type="hidden" name="item_id" value="{esc(item_id)}">
          <label>Actor ID</label><input name="actor_id" required placeholder="e.g. bac-member-01">
          <label>Role</label><input name="actor_role" value="BAC_MEMBER" list="roles">
          <label>Resolution</label><input name="resolution" value="ADVISORY_REVIEW_COMPLETE" required>
          <label>Note</label><textarea name="note"></textarea><button class="btn" type="submit">Resolve item</button>
          <p class="hint">{esc(_allowed_hint("RESOLVE"))}</p>
        </form>
        <form class="action-card" method="post" action="/action">{_token_field(auth_token)}
          <h3><span class="step">4</span>Override → correction</h3><input type="hidden" name="action" value="override"><input type="hidden" name="item_id" value="{esc(item_id)}">
          <label>Field path</label><input name="field_path" placeholder="documents.philgeps_registration.valid_until" required>
          <label>AI value (JSON literal)</label><input name="ai_value" placeholder='"2025-01-01"' required>
          <label>Correct value (JSON literal)</label><input name="correct_value" placeholder='"2027-01-01"' required>
          <label>Reason</label><textarea name="reason" required placeholder="Why this value is wrong"></textarea>
          <label>Submitted by (named human)</label><input name="submitted_by" required>
          <label>Role</label><input name="actor_role" value="BAC_STAFF" list="roles">
          <button class="btn" type="submit">Submit correction (PENDING)</button>
          <p class="hint">{esc(_allowed_hint("REQUEST_CORRECTION"))} · training-eligible only after 2nd review</p>
        </form>
        <form class="action-card" method="post" action="/action">{_token_field(auth_token)}
          <h3><span class="step">5</span>Adjudicate correction</h3><input type="hidden" name="action" value="adjudicate"><input type="hidden" name="item_id" value="{esc(item_id)}">
          <label>Correction ID</label><input name="correction_id" required placeholder="submission id from the queue">
          <label>Decision</label><select name="decision"><option>ACCEPTED</option><option>REJECTED</option></select>
          <label>Adjudicator (must differ from submitter)</label><input name="adjudicator" required>
          <label>Role</label><input name="actor_role" value="SECOND_REVIEWER" list="roles">
          <label>Note</label><textarea name="note"></textarea>
          <button class="btn btn-accent" type="submit">Adjudicate</button>
          <p class="hint">Allowed: {esc(", ".join(ADJUDICATE_ROLES))} · submitter can never self-adjudicate</p>
        </form>
      </aside>
    </div>
    {_role_datalist()}
    """
    return page(f"Review Item {it['id'][:8]}", body, message, error, active="queue")


def handle_post(conn, form):
    action = (form.get("action") or [""])[0]
    item_id = (form.get("item_id") or [""])[0]
    def f(key, default=""):
        return (form.get(key) or [default])[0]
    if action == "claim":
        claim_item(conn, item_id, f("actor_id"), actor_role=f("actor_role", "BAC_MEMBER"), note=f("note"))
        return item_id, "Item claimed (advisory workflow)."
    if action == "recommend":
        recommend(conn, item_id, f("actor_id"), f("actor_role", "BAC_MEMBER"), f("recommendation"), note=f("note"))
        return item_id, "Advisory recommendation recorded. No statutory decision was made."
    if action == "resolve":
        resolve_item(conn, item_id, f("actor_id"), f("actor_role", "BAC_MEMBER"), f("resolution"), note=f("note"))
        return item_id, "Item resolved in the advisory queue."
    if action == "override":
        corr = submit_override_correction(
            conn, item_id,
            field_path=f("field_path"),
            ai_value=json.loads(f("ai_value")),
            correct_value=json.loads(f("correct_value")),
            reason=f("reason"),
            submitted_by=f("submitted_by"),
            actor_role=f("actor_role", "BAC_STAFF"),
        )
        return item_id, f"Correction submitted as PENDING ({corr['correction_id']}). Second reviewer must adjudicate before training eligibility."
    if action == "adjudicate":
        res = adjudicate_override(conn, f("correction_id"), f("decision"), f("adjudicator"), f("note"),
                                  actor_role=f("actor_role", "SECOND_REVIEWER"))
        return item_id, f"Correction adjudicated: {res['status']}."
    raise CoreError(f"unknown action: {action}")


def _query_authed(params, auth_token):
    """True when a token query param (token/auth_token) matches."""
    if auth_token is None:
        return True
    for key in ("token", "auth_token"):
        provided = params.get(key) or ""
        if provided and hmac.compare_digest(provided, auth_token):
            return True
    return False


# Advisory action forms are tiny; cap the request body to bound memory/DoS.
MAX_POST_BYTES = 1024 * 1024

_DRAFT_DEFAULTS = _demo_params()


def _field(label, name, value="", required=False, input_type="text", size=28):
    req = " required" if required else ""
    return (f'<label>{label}</label>'
            f'<input name="{name}" type="{input_type}" value="{esc(value)}"'
            f' size="{size}"{req}>')


def _items_rows(prefix, rows, defaults):
    """Editable item grid rows (description/unit/qty/cost)."""
    out = ['<table class="draft-items"><thead><tr><th>#</th><th>Stock No.</th>'
           '<th>Description (one per line)</th><th>Unit</th><th>Qty</th>'
           '<th>Unit Cost</th></tr></thead><tbody>']
    for i in range(1, rows + 1):
        d = (defaults or {}).get(i, {})
        out.append(
            f'<tr><td>{i}</td>'
            f'<td><input name="{prefix}_stock_{i}" value="{esc(d.get("stock_no", ""))}" size="6"></td>'
            f'<td><textarea name="{prefix}_desc_{i}" rows="2" cols="34">{esc(d.get("description", ""))}</textarea></td>'
            f'<td><input name="{prefix}_unit_{i}" value="{esc(d.get("unit", ""))}" size="6"></td>'
            f'<td><input name="{prefix}_qty_{i}" value="{esc(d.get("quantity", ""))}" size="6"></td>'
            f'<td><input name="{prefix}_cost_{i}" value="{esc(d.get("unit_cost", ""))}" size="10"></td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def render_draft_form(result=None, error=None, auth_token=None):
    """End-user drafting form: transaction parameters -> full SVP chain PDFs.
    Defaults are the fictional demo scenario; every value is editable."""
    d = _DRAFT_DEFAULTS
    req = d["requested_by"]
    app = d["approved_by"]
    sup_rows = {i: {"supplier": s.get("supplier", ""),
                    "bid_amount": s.get("bid_amount", "")}
                for i, s in enumerate(d.get("suppliers", []), start=1)}
    awd = d.get("award", {})
    awd_items = {i: {"description": it.get("description", ""),
                     "unit": it.get("unit", ""), "quantity": it.get("quantity", ""),
                     "unit_cost": it.get("unit_cost", "")}
                 for i, it in enumerate(awd.get("items", []), start=1)}
    sup_html = ""
    for i in range(1, 5):
        s = sup_rows.get(i) or {}
        sup_html += (
            '<div class="sup-row">'
            + _field("Supplier " + str(i), "sup_name_" + str(i), s.get("supplier", ""))
            + _field("Total Bid (PHP)", "sup_bid_" + str(i), s.get("bid_amount", ""))
            + "</div>")
    body = f"""
    <div class="page-head">
      <div>
        <h1>SVP drafting</h1>
        <p>Transaction parameters in — the full SVP document chain out as watermarked, unsigned drafts.</p>
      </div>
      <span class="badge low">Fail-closed validation</span>
    </div>
    <div class="advisory">
      <span class="mark" aria-hidden="true">i</span>
      <span><strong>Drafts, never decisions.</strong> Drafts the full SVP chain (PR → RFQ → Minutes → Abstract →
      Resolution → PO) with computed amounts and ledger-issued numbers. A words/figures mismatch,
      fund-cluster drift, award to a non-lowest bidder or broken references <strong>blocks PDF rendering</strong>.
      Supplier bid amounts are human-entered canvass data — the system never invents prices. Every page is
      watermarked <strong>AI-ASSISTED DRAFT</strong> — not an official form until approved and signed.</span>
    </div>
    <form method="post" action="/draft">{_token_field(auth_token)}
      <div class="two-col">
        <fieldset class="step-card"><legend>Step 1 · Transaction</legend>
          {_field("Purpose / Project Title", "purpose", d["purpose"], required=True, size=60)}
          {_field("End User", "end_user", d["end_user"])}
          {_field("Office/Section", "office", d["office_section"])}
          {_field("Responsibility Center Code", "rc_code", d["responsibility_center_code"])}
          {_field("Fund Cluster", "fund_cluster", d["fund_cluster"], required=True)}
          {_field("PR Date", "pr_date", d["pr_date"], required=True, input_type="date")}
          {_field("RFQ Deadline Date", "deadline_date", d["deadline_date"], required=True, input_type="date")}
          {_field("Deadline Time", "deadline_time", d["deadline_time"])}
          {_field("Opening Date", "opening_date", d["opening_date"], input_type="date")}
          {_field("Opening Time", "opening_time", d["opening_time"])}
          {_field("Delivery Date", "delivery_date", d["delivery_date"], input_type="date")}
          {_field("Place of Delivery", "place", d["place_of_delivery"])}
          {_field("Payment Term", "payment_term", d["payment_term"], size=44)}
        </fieldset>
        <fieldset class="step-card"><legend>Step 2 · Officials (named humans)</legend>
          {_field("Requested by — Name", "req_name", req["name"], required=True)}
          {_field("Requested by — Designation", "req_desig", req["designation"], required=True)}
          {_field("Approved by — Name", "app_name", app["name"], required=True)}
          {_field("Approved by — Designation", "app_desig", app["designation"], required=True)}
          {_field("BAC Chairperson", "bac_chair", d["bac_chairperson"]["name"], required=True)}
          {_field("BAC Secretariat Head", "sec_head", d["secretariat_head"]["name"], required=True)}
          {_field("Contact Email", "email", d["contact_email"], size=30)}
          {_field("Accountant", "accountant", d["accountant"])}
        </fieldset>
      </div>
      <fieldset class="step-card"><legend>Step 3 · Items (PR lines — amounts are computed, never trusted)</legend>
        {_items_rows("item", 6, {i: {"stock_no": it.get("stock_no", ""),
                                     "description": it.get("description", ""),
                                     "unit": it.get("unit", ""),
                                     "quantity": it.get("quantity", ""),
                                     "unit_cost": it.get("unit_cost", "")}
                                  for i, it in enumerate(d["items"], start=1)})}
      </fieldset>
      <fieldset class="step-card"><legend>Step 4 · Canvass — supplier bids (blank to skip award docs)</legend>
        {sup_html}
        <h3>Award (optional — quoted prices from the canvass)</h3>
        {_field("Awarded Supplier (must be the lowest bid)", "award_supplier", awd.get("supplier", ""))}
        {_field("Supplier Representative", "award_rep", awd.get("representative", ""))}
        {_field("Supplier Address", "award_addr", awd.get("address", ""), size=40)}
        {_field("Supplier TIN", "award_tin", awd.get("tin", ""))}
        <p class="muted">Leave award item costs blank to reuse PR unit costs (then the PO
        total will differ from the supplier bids and validation will flag it — as it
        should).</p>
        {_items_rows("awd", 6, awd_items)}
      </fieldset>
      <fieldset class="step-card"><legend>Step 5 · Audit — you, the named human requesting this draft</legend>
        {_field("Your Actor ID", "actor_id", "", required=True)}
        {_field("Your Role", "actor_role", "BAC_STAFF", required=True)}
        <label class="chk"><input type="checkbox" name="allow_invalid" value="1">
          Render even if validation fails (every page stamped with a violations banner)</label>
        <button class="btn btn-accent" type="submit">Generate draft chain (6 PDFs)</button>
      </fieldset>
    </form>
    """
    if result:
        files_html = "".join(
            f'<li><a href="/draft-file?pr={esc(result["pr"])}&name={esc(os.path.basename(f))}">{esc(os.path.basename(f))}</a></li>'
            for f in result["files"])
        viol_html = ""
        if result["violations"]:
            viol_html = ("<p class='error'>Validation violations (drafts are stamped):</p><ul>"
                         + "".join(f"<li>{esc(v)}</li>" for v in result["violations"]) + "</ul>")
        notes_html = ""
        if result.get("notes"):
            notes_html = ("<p class='muted'>Advisory notes:</p><ul>"
                          + "".join(f"<li>{esc(n)}</li>" for n in result["notes"]) + "</ul>")
        body += f"""
        <section class="card card-pad section-gap">
          <h2>Draft chain generated — {esc(result["pr"])} (watermarked, unsigned)</h2>
          <p>Numbering: <span class="mono">{esc(canonical(result["numbering"]))}</span></p>
          {viol_html}{notes_html}
          <h3 style="margin-top:12px">Files</h3>
          <ul style="list-style:none;padding:0;display:grid;gap:6px">{files_html}</ul>
          <p class="muted">A DRAFT_GENERATED event was recorded in the ledger for
          actor {esc(result["actor"])}.</p>
        </section>"""
    return page("ProcEMP Drafting — SVP Document Chain", body, None, error, active="draft")


def _f(form, key, default=""):
    return ((form.get(key) or [default])[0] or default).strip()


def _items_from_form(form, prefix, count):
    items = []
    for i in range(1, count + 1):
        desc = _f(form, f"{prefix}_desc_{i}").replace("\r", "")
        if not desc:
            continue
        qty = _f(form, f"{prefix}_qty_{i}")
        cost = _f(form, f"{prefix}_cost_{i}")
        items.append({
            "stock_no": _f(form, f"{prefix}_stock_{i}"),
            "description": desc, "unit": _f(form, f"{prefix}_unit_{i}"),
            "quantity": float(qty) if qty else None,
            "unit_cost": float(cost) if cost else None,
        })
    return items


def handle_draft_post(conn, form, drafts_dir):
    """Build params from the form, draft the chain, render PDFs, record the
    ledger event. Returns a result dict for the form page. Raises CoreError
    on missing required fields; render failures (validation) propagate."""
    missing = [k for k in ("purpose", "pr_date", "deadline_date", "actor_id")
               if not _f(form, k)]
    if missing:
        raise CoreError("missing required fields: " + ", ".join(missing))
    items = _items_from_form(form, "item", 6)
    if not items:
        raise CoreError("at least one item line is required")
    params = {
        "entity_name": "Department of Information and Communications Technology",
        "header_address": "Regional Office 02 - Sample Address, Sample City",
        "end_user": _f(form, "end_user"),
        "office_section": _f(form, "office"),
        "responsibility_center_code": _f(form, "rc_code"),
        "fund_cluster": _f(form, "fund_cluster"),
        "purpose": _f(form, "purpose"),
        "mode_of_procurement": "Small Value Procurement",
        "pr_date": _f(form, "pr_date"),
        "deadline_date": _f(form, "deadline_date"),
        "deadline_time": _f(form, "deadline_time", "9:00 AM"),
        "opening_date": _f(form, "opening_date", _f(form, "deadline_date")),
        "opening_time": _f(form, "opening_time", "11:00 AM"),
        "delivery_date": _f(form, "delivery_date"),
        "place_of_delivery": _f(form, "place"),
        "payment_term": _f(form, "payment_term"),
        "accountant": _f(form, "accountant"),
        "contact_email": _f(form, "email"),
        "requested_by": {"name": _f(form, "req_name"),
                         "designation": _f(form, "req_desig")},
        "approved_by": {"name": _f(form, "app_name"),
                        "designation": _f(form, "app_desig")},
        "bac_chairperson": {"name": _f(form, "bac_chair")},
        "secretariat_head": {"name": _f(form, "sec_head")},
        "bac_members": [
            {"name": _f(form, "bac_chair"), "role": "BAC Chairperson"}],
        "items": items,
    }
    suppliers = []
    for i in range(1, 5):
        name = _f(form, f"sup_name_{i}")
        bid = _f(form, f"sup_bid_{i}")
        if name and bid:
            suppliers.append({"supplier": name, "bid_amount": float(bid)})
    if suppliers:
        params["suppliers"] = suppliers
    award_supplier = _f(form, "award_supplier")
    award_items = _items_from_form(form, "awd", 6)
    if award_supplier:
        award = {"supplier": award_supplier,
                 "representative": _f(form, "award_rep"),
                 "address": _f(form, "award_addr"), "tin": _f(form, "award_tin")}
        if award_items:
            award["items"] = award_items
        params["award"] = award

    docs, report = draft_chain(conn, params)
    allow_invalid = bool((form.get("allow_invalid") or ["0"])[0] == "1")
    written, manifest = render_pdfs(docs, drafts_dir, allow_invalid=allow_invalid)
    pr_no = report["numbering"]["PR"]
    append_event(conn, pr_no, "DRAFT_GENERATED", _f(form, "actor_id"),
                 _f(form, "actor_role", "BAC_STAFF"), {
                     "numbering": report["numbering"],
                     "purpose": params["purpose"],
                     "violations": report["violations"],
                     "files": [os.path.basename(f) for f in written],
                     "generated_at": report["generated_at"],
                 })
    result = {
        "pr": pr_no, "files": written, "violations": report["violations"],
        "notes": report.get("notes", []), "numbering": report["numbering"],
        "actor": _f(form, "actor_id"),
    }
    DRAFT_RESULTS[pr_no] = result
    return result


def _check_auth(headers, auth_token):
    """Return True when unauthenticated access should be rejected.

    auth_token=None means loopback-only prototype mode (no secret configured).
    Otherwise the caller must present a matching X-Auth-Token header.
    """
    if auth_token is None:
        return False
    provided = headers.get("X-Auth-Token", "") or ""
    return not hmac.compare_digest(provided, auth_token)


def make_handler(conn, *, auth_token=None, tls=False, drafts_dir="./drafts"):
    class Handler(BaseHTTPRequestHandler):
        def _denied(self):
            self._send(401, page("Unauthorized",
                                 "<p>X-Auth-Token required. Use the CLI for unauthenticated local work.</p>",
                                 error="401 Unauthorized"))

        def _send(self, code, body, content_type="text/html; charset=utf-8",
                  extra_headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or []):
                self.send_header(key, value)
            add_security_headers(self.send_header, tls=tls)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                if parsed.path == "/health":
                    self._send(200, canonical({"status": "ok", "advisory_only": True,
                                               "human_decision_required": True}),
                               "application/json; charset=utf-8")
                elif _check_auth(self.headers, auth_token) and not _query_authed(params, auth_token):
                    self._denied()
                elif parsed.path in ("/", "/queue"):
                    self._send(200, render_index(conn, params, params.get("msg"), params.get("err"), auth_token=auth_token))
                elif parsed.path == "/item":
                    self._send(200, render_item(conn, params["id"], params.get("msg"), params.get("err"), auth_token=auth_token))
                elif parsed.path == "/draft":
                    result = DRAFT_RESULTS.get(params.get("generated") or "")
                    self._send(200, render_draft_form(
                        result=result, error=params.get("err"),
                        auth_token=auth_token))
                elif parsed.path == "/draft-file":
                    result = DRAFT_RESULTS.get(params.get("pr") or "")
                    name = os.path.basename(params.get("name") or "")
                    allowed = {os.path.basename(f): f
                               for f in (result or {}).get("files", [])}
                    path = allowed.get(name)
                    if not path or not os.path.exists(path):
                        self._send(404, page(
                            "Not found",
                            "<p>Draft file not found (downloads are cleared on "
                            "server restart — regenerate the chain).</p>",
                            error="404 draft file not found"))
                    else:
                        with open(path, "rb") as fh:
                            self._send(200, fh.read(), "application/pdf",
                                       extra_headers=[("Content-Disposition",
                                                       f'attachment; filename="{name}"')])
                elif parsed.path == "/export":
                    # File export over HTTP disabled (arbitrary-write risk).
                    # Use: python review_queue.py export --out <path>
                    self._send(405, page("Method not allowed",
                                         "<p>HTTP export is disabled. Use the CLI: "
                                         "python review_queue.py export --out &lt;path&gt;</p>",
                                         error="405 Export via CLI only"))
                else:
                    self._send(404, page("Not found", "<p>Unknown path.</p>", error="404"))
            except Exception as exc:
                self._send(500, page("Error", f"<pre>{esc(exc)}</pre>", error=str(exc)))

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            raw = ""
            try:
                if parsed.path not in ("/action", "/draft"):
                    self._send(400, page("Error", "<p>Unknown POST path.</p>",
                                         error="unknown POST path"))
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length < 0:
                    raise CoreError("invalid Content-Length")
                if length > MAX_POST_BYTES:
                    self._send(413, page(
                        "Payload too large",
                        f"<p>Request body exceeds {MAX_POST_BYTES} bytes.</p>",
                        error="413 Payload too large"))
                    return
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                form = urllib.parse.parse_qs(raw)
                if _check_auth(self.headers, auth_token):
                    # Browser forms cannot set custom headers; accept a form
                    # field fallback checked with the same constant-time compare.
                    form_token = (form.get("auth_token") or [""])[0]
                    if not (auth_token is not None and form_token and
                            hmac.compare_digest(form_token, auth_token)):
                        self._denied()
                        return
                if parsed.path == "/draft":
                    result = handle_draft_post(conn, form, drafts_dir)
                    loc = (f"/draft?generated={urllib.parse.quote(result['pr'])}"
                           f"&msg={urllib.parse.quote('Draft chain generated (unsigned).')}")
                    self.send_response(303)
                    self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    add_security_headers(self.send_header, tls=tls)
                    self.end_headers()
                    return
                item_id, msg = handle_post(conn, form)
                loc = f"/item?id={urllib.parse.quote(item_id)}&msg={urllib.parse.quote(msg)}"
                self.send_response(303)
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                add_security_headers(self.send_header, tls=tls)
                self.end_headers()
            except Exception as exc:
                target = ("/draft" if parsed.path == "/draft" else None)
                item_id = (urllib.parse.parse_qs(raw).get("item_id") or [""])[0] if 'raw' in locals() else ""
                if target:
                    loc = f"{target}?err={urllib.parse.quote(str(exc))}"
                    self.send_response(303)
                    self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    add_security_headers(self.send_header, tls=tls)
                    self.end_headers()
                elif item_id:
                    loc = f"/item?id={urllib.parse.quote(item_id)}&err={urllib.parse.quote(str(exc))}"
                    self.send_response(303)
                    self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    add_security_headers(self.send_header, tls=tls)
                    self.end_headers()
                else:
                    self._send(400, page("Error", f"<pre>{esc(exc)}</pre>", error=str(exc)))

        def log_message(self, fmt, *args):
            sys.stderr.write("[review-ui] " + (fmt % args) + "\n")

    return Handler


def serve(conn, host="127.0.0.1", port=8091, *, auth_token=None,
          tls_cert=None, tls_key=None, tls_client_ca=None,
          drafts_dir="./drafts"):
    _assert_loopback(host)
    tls_context = build_tls_context(tls_cert, tls_key, tls_client_ca)
    server = ThreadingHTTPServer((host, port),
                                 make_handler(conn, auth_token=auth_token,
                                              tls=tls_context is not None,
                                              drafts_dir=drafts_dir))
    wrap_server(server, tls_context)
    scheme = scheme_for(tls_context)
    print(f"ProcEMP advisory review UI on {scheme}://{host}:{port} (loopback only)")
    print("Advisory only: authorized humans make statutory procurement decisions.")
    if tls_context is not None:
        print("TLS ENABLED (TLS 1.2+)"
              + ("; mutual TLS required (client certificate)." if tls_client_ca else "."))
    if auth_token:
        print("Shared-secret auth ENABLED (X-Auth-Token required except /health).")
    else:
        print("WARNING: no auth token configured; loopback-only prototype. "
              "Put an authenticated reverse proxy in front before any LAN access.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="ProcEMP advisory review web UI (loopback only)")
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--drafts-dir", default="./drafts",
                        help="Directory for generated draft PDFs")
    parser.add_argument("--auth-token", default=os.environ.get("REVIEW_AUTH_TOKEN"),
                        help="Shared secret; falls back to REVIEW_AUTH_TOKEN env")
    add_tls_arguments(parser, env_prefix="REVIEW")
    args = parser.parse_args(argv)
    conn = _connect(args.db)
    try:
        serve(conn, args.host, args.port, auth_token=args.auth_token,
              tls_cert=args.tls_cert, tls_key=args.tls_key,
              tls_client_ca=args.tls_client_ca, drafts_dir=args.drafts_dir)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())