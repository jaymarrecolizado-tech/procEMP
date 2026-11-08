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

from core import CoreError, _connect, canonical, now
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


def page(title, body, message=None, error=None):
    flash = ""
    if message:
        flash += f'<div class="flash ok">{esc(message)}</div>'
    if error:
        flash += f'<div class="flash bad">{esc(error)}</div>'
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root{{--bg:#f6f8fb;--card:#fff;--ink:#16202b;--muted:#5d6b7a;--line:#d8e1ec;--warn:#8a4b00;--bad:#8f1d1d;--ok:#1f5f3b;--info:#1f4f8f}}
*{{box-sizing:border-box}} body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}}
header{{background:#fff;border-bottom:1px solid var(--line);padding:18px 22px}} main{{padding:18px 22px;max-width:1200px;margin:0 auto}}
h1,h2,h3{{margin:0 0 8px}} a{{color:var(--info)}} .muted{{color:var(--muted)}}
.notice{{margin-top:10px;padding:12px;border:1px solid #f0d9a8;background:#fff8e8;color:var(--warn);border-radius:10px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}}
.card,section,form.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}}
.num{{font-size:26px;font-weight:700}} table{{width:100%;border-collapse:collapse}} th,td{{padding:8px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left;font-size:14px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line);background:#f2f5f8;margin-right:4px}}
.abstain,.fail,.open,.high,.critical{{color:var(--bad);border-color:#efcaca;background:#fff3f3}}
.pass,.resolved{{color:var(--ok);border-color:#cde6d8;background:#f2fbf6}}
.claimed,.escalated{{color:var(--info);border-color:#cfe0f5;background:#f3f8ff}}
pre{{white-space:pre-wrap;word-break:break-word;background:#fbfcfd;border:1px solid var(--line);padding:10px;border-radius:8px;font-size:12px}}
label{{display:block;font-size:13px;color:var(--muted);margin-top:8px}} input,select,textarea{{width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;font:inherit}}
textarea{{min-height:70px}} button{{margin-top:10px;padding:9px 14px;border:0;border-radius:8px;background:var(--info);color:#fff;font:inherit;cursor:pointer}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}
.flash{{padding:10px 12px;border-radius:10px;margin:12px 0}} .flash.ok{{background:#f2fbf6;border:1px solid #cde6d8;color:var(--ok)}} .flash.bad{{background:#fff3f3;border:1px solid #efcaca;color:var(--bad)}}
details{{margin-top:8px}} summary{{cursor:pointer;color:var(--info)}}
</style></head><body>
<header>
  <h1>{esc(title)}</h1>
  <div class="muted"><a href="/">Queue</a> | <a href="/?status=OPEN">Open</a> | <a href="/?status=CLAIMED">Claimed</a> | Generated {esc(now())}</div>
  <div class="notice"><strong>Advisory decision-support UI.</strong> AI flags/suggestions do <strong>not</strong> make statutory procurement decisions.
  Authorized humans (BAC/TWG/HOPE/Inspection/Accounting as applicable) must review evidence and record/sign official determinations through the governed workflow.</div>
</header>
<main>{flash}{body}</main>
</body></html>"""


def render_index(conn, params, message=None, error=None, auth_token=None):
    status = params.get("status") or None
    dash = queue_dashboard(conn)
    items = list_queue(conn, status=status, limit=200)["items"]
    rows = []
    for it in items:
        rows.append(
            "<tr>"
            f"<td><a href='/item?id={esc(it['id'])}'>{esc(it['id'][:8])}</a></td>"
            f"<td>{esc(it.get('tx_number') or it.get('tx_id'))}</td>"
            f"<td><span class='badge {esc((it.get('decision') or '').lower())}'>{esc(it.get('decision'))}</span></td>"
            f"<td>{esc(it.get('confidence'))}</td>"
            f"<td><span class='badge {esc(it['status'].lower())}'>{esc(it['status'])}</span> <span class='badge'>{esc(it['reason'])}</span></td>"
            f"<td>{esc(it.get('missing_documents'))}</td><td>{esc(it.get('invalid_documents'))}</td>"
            f"<td>{esc(it.get('assigned_to') or '')}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Item</th><th>Tx</th><th>Decision</th><th>Conf.</th><th>Status/Reason</th><th>Missing</th><th>Invalid</th><th>Assigned</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=8 class=muted>No items.</td></tr>'}</tbody></table>"
    )
    body = f"""
    <div class="cards">
      <div class="card"><div class="muted">Open</div><div class="num">{dash['open_items']}</div></div>
      <div class="card"><div class="muted">Claimed</div><div class="num">{dash['claimed_items']}</div></div>
      <div class="card"><div class="muted">Escalated</div><div class="num">{dash['escalated_items']}</div></div>
      <div class="card"><div class="muted">Resolved</div><div class="num">{dash['resolved_items']}</div></div>
    </div>
    <section><h2>Queue {esc(f'(status={status})' if status else '')}</h2>{table}</section>
    <form class="panel" method="get" action="/">{_token_field(auth_token)}<label>Filter status</label>
      <select name="status"><option value="">All</option>
      {"".join(f'<option {"selected" if status==s else ""}>{s}</option>' for s in ['OPEN','CLAIMED','ESCALATED','RESOLVED'])}
      </select><button type="submit">Apply</button></form>
    """
    return page("ProcEMP Advisory Review Queue", body, message, error)


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
            status = f"{'verified' if ok else 'FAILED: ' + reason}"
        else:
            status = "unverified — no chunk_id"
        rows_html.append(
            f"<tr><td>{esc(c.get('section'))}</td>"
            f"<td>{esc(c.get('chunk_id') or '—')}</td>"
            f"<td>{esc(passage or '—')}</td><td>{esc(status)}</td></tr>"
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
    actions_html = "".join(
        f"<li>{esc(a['created_at'])} — <strong>{esc(a['action'])}</strong> by {esc(a['actor_id'])} ({esc(a['actor_role'])}) {esc(a.get('note') or '')}"
        f"<pre>{esc(canonical(a.get('payload'))) }</pre></li>" for a in it.get("actions") or []
    ) or "<li class='muted'>No actions yet.</li>"
    body = f"""
    <section>
      <h2>Item {esc(it['id'])}</h2>
      <p><strong>Tx:</strong> {esc(it.get('tx_number') or it['tx_id'])} | <strong>Mode/Stage:</strong> {esc(it.get('tx_mode'))}/{esc(it.get('tx_stage'))}
         | <strong>Status:</strong> <span class='badge {esc(it['status'].lower())}'>{esc(it['status'])}</span>
         | <strong>Reason:</strong> {esc(it['reason'])} | <strong>Priority:</strong> {esc(it['priority'])}
         | <strong>Assigned:</strong> {esc(it.get('assigned_to') or '—')}</p>
      <p><strong>Verdict:</strong> {esc(it['verdict_id'])} | <strong>Decision (advisory):</strong> <span class='badge {esc((v.get('decision') or '').lower())}'>{esc(v.get('decision'))}</span>
         | <strong>Confidence:</strong> {esc(v.get('confidence'))} | <strong>Model:</strong> {esc(v.get('model_version'))}</p>
      <p><strong>Missing:</strong> {esc(v.get('missing_documents'))} | <strong>Invalid:</strong> {esc(v.get('invalid_documents'))}</p>
      <div class="notice"><strong>Suggested next step (advisory):</strong> Verify evidence and source documents, then record an advisory action. Any statutory determination requires the authorized human body and governed sign-off.</div>
      <h3>Provenance (advisory lineage)</h3>
      <p><strong>Regime/Ruleset:</strong> {esc(it.get('tx_regime'))} / {esc(v.get('ruleset_version'))}
         | <strong>Corpus snapshot:</strong> {esc(v.get('corpus_snapshot_id'))}
         | <strong>Prompt hash:</strong> <code>{esc(v.get('prompt_hash'))}</code>
         | <strong>Rules engine:</strong> {esc(v.get('rules_engine_version'))}</p>
      <h3>Evidence spans</h3>
      <table><thead><tr><th>Field</th><th>Value</th><th>Page</th><th>Snippet</th></tr></thead><tbody>
      {''.join(f"<tr><td>{esc(s.get('field'))}</td><td>{esc(s.get('value'))}</td><td>{esc(s.get('page',''))}</td><td>{esc(s.get('snippet',''))}</td></tr>" for s in (v.get('evidence_spans') or []) if isinstance(s, dict)) or '<tr><td colspan=4 class="muted">No evidence spans on this verdict.</td></tr>'}
      </tbody></table>
      <details open><summary>Notes</summary><pre>{esc(canonical(v.get('notes', [])))}</pre></details>
      <h3>Rule citations (passage + verification)</h3>
      {_citations_table(conn, it["tx_id"], v.get("rule_citations", []))}
      <details><summary>Rule citations (raw JSON)</summary><pre>{esc(canonical(v.get('rule_citations', [])))}</pre></details>
      <h3>Ingested documents (metadata only, sealed-bid custody enforced)</h3>
      {_documents_table(conn, it["tx_id"])}
      <h3>Ledger timeline (advisory audit trail)</h3>
      {_events_table(conn, it["tx_id"])}
      <details><summary>Full verdict payload</summary><pre>{esc(canonical(v))}</pre></details>
      <h3>Recorded actions</h3><ul>{actions_html}</ul>
    </section>
    {_role_datalist()}
    <div class="grid">
      <form class="panel" method="post" action="/action">{_token_field(auth_token)}
        <h3>Claim</h3><input type="hidden" name="action" value="claim"><input type="hidden" name="item_id" value="{esc(item_id)}">
        <label>Actor ID (named human)</label><input name="actor_id" required>
        <label>Role</label><input name="actor_role" value="BAC_MEMBER" list="roles">
        <label>Note</label><textarea name="note"></textarea><button type="submit">Claim item</button>
        <p class="muted">{esc(_allowed_hint("CLAIM"))}</p>
      </form>
      <form class="panel" method="post" action="/action">{_token_field(auth_token)}
        <h3>Advisory recommendation</h3><input type="hidden" name="action" value="recommend"><input type="hidden" name="item_id" value="{esc(item_id)}">
        <label>Actor ID</label><input name="actor_id" required>
        <label>Role</label><input name="actor_role" value="BAC_MEMBER" list="roles">
        <label>Recommendation</label><select name="recommendation">{"".join(f'<option>{r}</option>' for r in sorted(ADVISORY_RECOMMENDATIONS))}</select>
        <label>Note</label><textarea name="note"></textarea><button type="submit">Record recommendation</button>
        <p class="muted">{esc(_allowed_hint("RECOMMEND_REVIEW"))} (per recommendation)</p>
      </form>
      <form class="panel" method="post" action="/action">{_token_field(auth_token)}
        <h3>Resolve (advisory workflow)</h3><input type="hidden" name="action" value="resolve"><input type="hidden" name="item_id" value="{esc(item_id)}">
        <label>Actor ID</label><input name="actor_id" required>
        <label>Role</label><input name="actor_role" value="BAC_MEMBER" list="roles">
        <label>Resolution</label><input name="resolution" value="ADVISORY_REVIEW_COMPLETE" required>
        <label>Note</label><textarea name="note"></textarea><button type="submit">Resolve item</button>
        <p class="muted">{esc(_allowed_hint("RESOLVE"))}</p>
      </form>
      <form class="panel" method="post" action="/action">{_token_field(auth_token)}
        <h3>Override → correction (training-eligible only after 2nd review)</h3>
        <input type="hidden" name="action" value="override"><input type="hidden" name="item_id" value="{esc(item_id)}">
        <label>Field path</label><input name="field_path" placeholder="documents.philgeps_registration.valid_until" required>
        <label>AI value (JSON literal)</label><input name="ai_value" placeholder='"2025-01-01"' required>
        <label>Correct value (JSON literal)</label><input name="correct_value" placeholder='"2027-01-01"' required>
        <label>Reason</label><textarea name="reason" required></textarea>
        <label>Submitted by (named human)</label><input name="submitted_by" required>
        <label>Role</label><input name="actor_role" value="BAC_STAFF" list="roles">
        <button type="submit">Submit correction (PENDING)</button>
        <p class="muted">{esc(_allowed_hint("REQUEST_CORRECTION"))}</p>
      </form>
      <form class="panel" method="post" action="/action">{_token_field(auth_token)}
        <h3>Adjudicate correction (second reviewer)</h3>
        <input type="hidden" name="action" value="adjudicate"><input type="hidden" name="item_id" value="{esc(item_id)}">
        <label>Correction ID</label><input name="correction_id" required>
        <label>Decision</label><select name="decision"><option>ACCEPTED</option><option>REJECTED</option></select>
        <label>Adjudicator (must differ from submitter)</label><input name="adjudicator" required>
        <label>Role</label><input name="actor_role" value="SECOND_REVIEWER" list="roles">
        <label>Note</label><textarea name="note"></textarea>
        <button type="submit">Adjudicate</button>
        <p class="muted">Allowed: {esc(", ".join(ADJUDICATE_ROLES))} (submitter can never self-adjudicate)</p>
      </form>
    </div>
    """
    return page(f"Review Item {it['id'][:8]}", body, message, error)


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


def _check_auth(headers, auth_token):
    """Return True when unauthenticated access should be rejected.

    auth_token=None means loopback-only prototype mode (no secret configured).
    Otherwise the caller must present a matching X-Auth-Token header.
    """
    if auth_token is None:
        return False
    provided = headers.get("X-Auth-Token", "") or ""
    return not hmac.compare_digest(provided, auth_token)


def make_handler(conn, *, auth_token=None, tls=False):
    class Handler(BaseHTTPRequestHandler):
        def _denied(self):
            self._send(401, page("Unauthorized",
                                 "<p>X-Auth-Token required. Use the CLI for unauthenticated local work.</p>",
                                 error="401 Unauthorized"))

        def _send(self, code, body, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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
                if parsed.path != "/action":
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
                item_id, msg = handle_post(conn, form)
                loc = f"/item?id={urllib.parse.quote(item_id)}&msg={urllib.parse.quote(msg)}"
                self.send_response(303)
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                add_security_headers(self.send_header, tls=tls)
                self.end_headers()
            except Exception as exc:
                item_id = (urllib.parse.parse_qs(raw).get("item_id") or [""])[0] if 'raw' in locals() else ""
                if item_id:
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
          tls_cert=None, tls_key=None, tls_client_ca=None):
    _assert_loopback(host)
    tls_context = build_tls_context(tls_cert, tls_key, tls_client_ca)
    server = ThreadingHTTPServer((host, port),
                                 make_handler(conn, auth_token=auth_token,
                                              tls=tls_context is not None))
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
    parser.add_argument("--auth-token", default=os.environ.get("REVIEW_AUTH_TOKEN"),
                        help="Shared secret; falls back to REVIEW_AUTH_TOKEN env")
    add_tls_arguments(parser, env_prefix="REVIEW")
    args = parser.parse_args(argv)
    conn = _connect(args.db)
    try:
        serve(conn, args.host, args.port, auth_token=args.auth_token,
              tls_cert=args.tls_cert, tls_key=args.tls_key,
              tls_client_ca=args.tls_client_ca)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())