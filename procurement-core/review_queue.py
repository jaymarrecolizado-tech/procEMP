"""BAC review queue for AI-assisted (advisory) procurement checks.

Surfaces verdicts that need human attention (ABSTAIN, FAIL, or flagged cases)
with evidence, citations, and extracted context. The queue records *advisory*
review actions only: it never makes or finalizes a statutory procurement
decision. Named human bodies (BAC/TWG/HOPE/etc.) decide and sign through the
system's gate/sign-off controls.

Overrides that should improve the extraction model are submitted as corrections
via ``learning.submit_correction``; they remain PENDING until a different
second reviewer adjudicates them (``learning.adjudicate_correction``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from core import CoreError, append_event, canonical, get_tx, init_db, now, uid
from learning import adjudicate_correction, submit_correction

VALID_ITEM_STATUS = ("OPEN", "CLAIMED", "RESOLVED", "ESCALATED")
VALID_PRIORITY = ("LOW", "NORMAL", "HIGH", "CRITICAL")
VALID_ACTIONS = (
    "CLAIM",
    "ACK",
    "RECOMMEND_CONFIRM",
    "RECOMMEND_REJECT",
    "RECOMMEND_REVIEW",
    "REQUEST_CORRECTION",
    "ESCALATE",
    "RESOLVE",
    "NOTE",
)
ADVISORY_RECOMMENDATIONS = {
    "CONFIRM_AI": "RECOMMEND_CONFIRM",
    "REJECT_AI": "RECOMMEND_REJECT",
    "NEEDS_HUMAN_REVIEW": "RECOMMEND_REVIEW",
    "ESCALATE": "ESCALATE",
}

# RBAC matrix (Track C hardening). Advisory queue only — these roles gate
# workflow actions, never statutory decisions (those need governed sign-offs).
# - CLAIM/ACK/NOTE: any named reviewer role.
# - RECOMMEND_*: BAC members/chair/secretariat/TWG only (not generic staff).
# - RESOLVE: BAC members/chair/secretariat only (closes the advisory item).
# - REQUEST_CORRECTION (override): BAC_STAFF/SECRETARIAT/TECHNICAL_WORKING_GROUP.
# - Adjudication (training eligibility) is enforced in learning.py SoD +
#   the role check in adjudicate_override() below.
ROLE_PERMISSIONS = {
    "CLAIM": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT", "BAC_STAFF",
              "TECHNICAL_WORKING_GROUP", "SECOND_REVIEWER"),
    "ACK": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT", "BAC_STAFF",
            "TECHNICAL_WORKING_GROUP", "SECOND_REVIEWER"),
    "NOTE": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT", "BAC_STAFF",
             "TECHNICAL_WORKING_GROUP", "SECOND_REVIEWER"),
    "RECOMMEND_CONFIRM": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT",
                         "TECHNICAL_WORKING_GROUP", "HOPE"),
    "RECOMMEND_REJECT": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT",
                        "TECHNICAL_WORKING_GROUP", "HOPE"),
    "RECOMMEND_REVIEW": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT",
                        "TECHNICAL_WORKING_GROUP", "HOPE"),
    "ESCALATE": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT",
                 "TECHNICAL_WORKING_GROUP"),
    "RESOLVE": ("BAC_MEMBER", "BAC_CHAIR", "BAC_SECRETARIAT"),
    "REQUEST_CORRECTION": ("BAC_STAFF", "BAC_SECRETARIAT",
                           "TECHNICAL_WORKING_GROUP"),
}

ADJUDICATE_ROLES = ("SECOND_REVIEWER", "BAC_CHAIR", "BAC_SECRETARIAT")


def require_role(action, actor_role):
    """Raise CoreError unless actor_role may perform `action` (RBAC matrix)."""
    allowed = ROLE_PERMISSIONS.get(action)
    if allowed is None:
        raise CoreError(f"unknown review action: {action}")
    if actor_role not in allowed:
        raise CoreError(
            f"role '{actor_role}' may not perform '{action}' "
            f"(allowed: {', '.join(allowed)})"
        )


def _j(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse(text, default=None):
    if not text:
        return default
    return json.loads(text)


def _row(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _verdict_row(conn, verdict_id):
    row = _row(conn, "SELECT * FROM verdict WHERE id=?", (verdict_id,))
    if not row:
        raise CoreError("unknown verdict")
    row["payload"] = _parse(row["payload"], {})
    return row


def _reason_from_verdict(verdict):
    decision = verdict.get("decision") or verdict.get("status")
    if decision == "ABSTAIN":
        return "ABSTAIN"
    if decision == "FAIL":
        return "FAIL"
    if verdict.get("confidence", 1) < 0.75:
        return "LOW_CONFIDENCE"
    if verdict.get("invalid_documents") or verdict.get("missing_documents"):
        return "FLAG"
    return "REVIEW"


def enqueue_verdict(conn, tx_id, verdict_id, *, created_by, reason=None, priority=None):
    """Add an advisory review item for a verdict. Idempotent per (tx, verdict)."""
    if not created_by:
        raise CoreError("created_by is required")
    get_tx(conn, tx_id)  # validates tx
    vrow = _verdict_row(conn, verdict_id)
    if vrow["tx_id"] != tx_id:
        raise CoreError("verdict does not belong to tx_id")
    existing = _row(
        conn,
        "SELECT * FROM review_item WHERE tx_id=? AND verdict_id=?",
        (tx_id, verdict_id),
    )
    if existing:
        existing["advisory"] = True
        existing["existing"] = True
        return existing

    verdict = vrow["payload"]
    reason = reason or _reason_from_verdict(verdict)
    priority = priority or ("HIGH" if reason in ("ABSTAIN", "FAIL") else "NORMAL")
    if priority not in VALID_PRIORITY:
        raise CoreError(f"priority must be one of {VALID_PRIORITY}")

    item_id = uid()
    created_at = now()
    conn.execute(
        "INSERT INTO review_item "
        "(id, tx_id, verdict_id, status, reason, priority, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (item_id, tx_id, verdict_id, "OPEN", reason, priority, created_at, created_by),
    )
    append_event(conn, tx_id, "REVIEW_ITEM_ENQUEUED", created_by, "REVIEW_QUEUE", {
        "review_item_id": item_id,
        "verdict_id": verdict_id,
        "reason": reason,
        "priority": priority,
        "decision": verdict.get("decision"),
        "advisory": True,
        "note": "AI output is advisory; a named human body must make any statutory determination",
    })
    return {
        "id": item_id,
        "tx_id": tx_id,
        "verdict_id": verdict_id,
        "status": "OPEN",
        "reason": reason,
        "priority": priority,
        "created_at": created_at,
        "created_by": created_by,
        "advisory": True,
        "existing": False,
    }


def list_queue(conn, *, status=None, tx_id=None, assigned_to=None, limit=100):
    clauses = []
    params = []
    if status:
        clauses.append("ri.status = ?")
        params.append(status)
    if tx_id:
        clauses.append("ri.tx_id = ?")
        params.append(tx_id)
    if assigned_to:
        clauses.append("ri.assigned_to = ?")
        params.append(assigned_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    rows = _rows(
        conn,
        "SELECT ri.*, tx.number AS tx_number, tx.stage AS tx_stage, tx.mode AS tx_mode, "
        "v.payload AS verdict_payload, v.created_at AS verdict_created_at "
        "FROM review_item ri "
        "JOIN proc_tx tx ON tx.id = ri.tx_id "
        "JOIN verdict v ON v.id = ri.verdict_id "
        f"{where} "
        "ORDER BY CASE ri.priority "
        "WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'NORMAL' THEN 2 ELSE 3 END, "
        "ri.created_at DESC LIMIT ?",
        params,
    )
    items = []
    for r in rows:
        verdict = _parse(r.pop("verdict_payload", "{}"), {})
        items.append({
            "id": r["id"],
            "tx_id": r["tx_id"],
            "tx_number": r.get("tx_number"),
            "tx_stage": r.get("tx_stage"),
            "tx_mode": r.get("tx_mode"),
            "verdict_id": r["verdict_id"],
            "verdict_created_at": r.get("verdict_created_at"),
            "decision": verdict.get("decision"),
            "confidence": verdict.get("confidence"),
            "missing_documents": verdict.get("missing_documents", []),
            "invalid_documents": verdict.get("invalid_documents", []),
            "status": r["status"],
            "reason": r["reason"],
            "priority": r["priority"],
            "assigned_to": r.get("assigned_to"),
            "created_at": r["created_at"],
            "created_by": r["created_by"],
            "resolved_at": r.get("resolved_at"),
            "resolution": r.get("resolution"),
            "advisory": True,
        })
    return {"items": items, "count": len(items), "advisory": True}


def get_item(conn, item_id):
    item = _row(
        conn,
        "SELECT ri.*, tx.number AS tx_number, tx.stage AS tx_stage, tx.mode AS tx_mode, "
        "tx.regime AS tx_regime, tx.posting_date AS tx_posting_date "
        "FROM review_item ri JOIN proc_tx tx ON tx.id = ri.tx_id WHERE ri.id=?",
        (item_id,),
    )
    if not item:
        raise CoreError("unknown review item")
    verdict_row = _verdict_row(conn, item["verdict_id"])
    actions = _rows(
        conn,
        "SELECT * FROM review_action WHERE review_item_id=? ORDER BY created_at",
        (item_id,),
    )
    for a in actions:
        a["payload"] = _parse(a.pop("payload_json", None), None)
    item["verdict"] = verdict_row["payload"]
    item["verdict_created_at"] = verdict_row["created_at"]
    item["actions"] = actions
    item["advisory"] = True
    item["notice"] = (
        "AI output is advisory. This queue does not make statutory procurement "
        "decisions; BAC/TWG/HOPE/authorized bodies must decide and sign."
    )
    return item


def _get_item_row(conn, item_id):
    item = _row(conn, "SELECT * FROM review_item WHERE id=?", (item_id,))
    if not item:
        raise CoreError("unknown review item")
    return item


def claim_item(conn, item_id, actor_id, *, actor_role="BAC_MEMBER", note=""):
    if not actor_id:
        raise CoreError("actor_id is required")
    item = _get_item_row(conn, item_id)
    if item["status"] == "RESOLVED":
        raise CoreError("resolved review items cannot be claimed")
    conn.execute(
        "UPDATE review_item SET status='CLAIMED', assigned_to=? WHERE id=?",
        (actor_id, item_id),
    )
    record_action(conn, item_id, "CLAIM", actor_id, actor_role, note=note)
    append_event(conn, item["tx_id"], "REVIEW_ITEM_CLAIMED", actor_id, actor_role, {
        "review_item_id": item_id,
        "verdict_id": item["verdict_id"],
        "advisory": True,
    })
    return get_item(conn, item_id)


def record_action(conn, item_id, action, actor_id, actor_role, *, note="",
                  payload=None, resolve=False, resolution=None):
    if action not in VALID_ACTIONS:
        raise CoreError(f"action must be one of {VALID_ACTIONS}")
    if not actor_id or not actor_role:
        raise CoreError("actor_id and actor_role are required")
    require_role(action, actor_role)
    item = _get_item_row(conn, item_id)
    aid = uid()
    created_at = now()
    conn.execute(
        "INSERT INTO review_action "
        "(id, review_item_id, tx_id, action, actor_id, actor_role, note, payload_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, item_id, item["tx_id"], action, actor_id, actor_role, note or None,
         _j(payload) if payload is not None else None, created_at),
    )
    if resolve:
        _resolve_row(conn, item, actor_id, resolution or action, note)
    append_event(conn, item["tx_id"], "REVIEW_ACTION_RECORDED", actor_id, actor_role, {
        "review_item_id": item_id,
        "review_action_id": aid,
        "action": action,
        "resolution": resolution if resolve else None,
        "advisory": True,
    })
    return {"review_action_id": aid, "action": action, "created_at": created_at}


def _resolve_row(conn, item, actor_id, resolution, note):
    if item["status"] == "RESOLVED":
        return
    if not resolution:
        raise CoreError("resolution is required")
    conn.execute(
        "UPDATE review_item SET status=?, resolved_at=?, resolved_by=?, "
        "resolution=?, resolution_note=? WHERE id=?",
        ("ESCALATED" if resolution == "ESCALATED" else "RESOLVED",
         now(), actor_id, resolution, note or None, item["id"]),
    )


def recommend(conn, item_id, actor_id, actor_role, recommendation, *, note=""):
    """Record an advisory recommendation. Never a statutory determination."""
    if recommendation not in ADVISORY_RECOMMENDATIONS:
        raise CoreError(
            f"recommendation must be one of {sorted(ADVISORY_RECOMMENDATIONS)}"
        )
    if not actor_id or not actor_role:
        raise CoreError("actor_id and actor_role are required")
    action = ADVISORY_RECOMMENDATIONS[recommendation]
    resolve = recommendation in ("CONFIRM_AI", "REJECT_AI")
    status = "ESCALATED" if recommendation == "ESCALATE" else None
    rec = record_action(
        conn, item_id, action, actor_id, actor_role,
        note=note,
        payload={"recommendation": recommendation, "advisory": True},
        resolve=resolve or recommendation == "ESCALATE",
        resolution=status or ("ADVISORY_" + recommendation if resolve else None),
    )
    if recommendation == "ESCALATE":
        conn.execute("UPDATE review_item SET status='ESCALATED' WHERE id=?", (item_id,))
    append_event(conn, _get_item_row(conn, item_id)["tx_id"],
                 "REVIEW_RECOMMENDATION_RECORDED", actor_id, actor_role, {
                     "review_item_id": item_id,
                     "recommendation": recommendation,
                     "advisory": True,
                     "note": note,
                 })
    return rec


def resolve_item(conn, item_id, actor_id, actor_role, resolution, *, note=""):
    if not resolution:
        raise CoreError("resolution is required")
    rec = record_action(conn, item_id, "RESOLVE", actor_id, actor_role,
                        note=note, payload={"resolution": resolution},
                        resolve=True, resolution=resolution)
    append_event(conn, _get_item_row(conn, item_id)["tx_id"], "REVIEW_ITEM_RESOLVED",
                 actor_id, actor_role, {
                     "review_item_id": item_id,
                     "resolution": resolution,
                     "advisory": True,
                 })
    return rec


def submit_override_correction(conn, item_id, *, field_path, ai_value,
                               correct_value, reason, submitted_by,
                               actor_role="BAC_STAFF"):
    """Submit a correction from the queue. Correction is NOT training data until
    a second reviewer adjudicates it via learning.adjudicate_correction."""
    if not submitted_by:
        raise CoreError("submitted_by is required")
    item = _get_item_row(conn, item_id)
    correction = submit_correction(
        conn,
        item["tx_id"],
        verdict_id=item["verdict_id"],
        field_path=field_path,
        ai_value=ai_value,
        correct_value=correct_value,
        reason=reason,
        submitted_by=submitted_by,
    )
    record_action(
        conn, item_id, "REQUEST_CORRECTION", submitted_by, actor_role,
        note=reason,
        payload={"correction_id": correction["correction_id"], "field_path": field_path,
                 "status": correction["status"]},
    )
    append_event(conn, item["tx_id"], "REVIEW_CORRECTION_SUBMITTED", submitted_by,
                 actor_role, {
                     "review_item_id": item_id,
                     "correction_id": correction["correction_id"],
                     "requires_second_reviewer_adjudication": True,
                     "advisory": True,
                 })
    return correction


def adjudicate_override(conn, correction_id, decision, adjudicator, note="",
                        actor_role="SECOND_REVIEWER"):
    """Convenience wrapper; enforces RBAC + segregation of duties.

    The submitter can never adjudicate their own correction (learning.py),
    and only an adjudicator role may call this path (RBAC matrix).
    """
    if actor_role not in ADJUDICATE_ROLES:
        raise CoreError(
            f"role '{actor_role}' may not adjudicate corrections "
            f"(allowed: {', '.join(ADJUDICATE_ROLES)})"
        )
    return adjudicate_correction(conn, correction_id, decision, adjudicator, note)


def queue_dashboard(conn):
    def count(sql, params=()):
        return conn.execute(sql, params).fetchone()["c"]

    by_reason = _rows(
        conn,
        "SELECT reason, count(*) AS c FROM review_item "
        "GROUP BY reason ORDER BY c DESC",
    )
    by_status = _rows(
        conn,
        "SELECT status, count(*) AS c FROM review_item "
        "GROUP BY status ORDER BY c DESC",
    )
    return {
        "open_items": count("SELECT count(*) c FROM review_item WHERE status='OPEN'"),
        "claimed_items": count("SELECT count(*) c FROM review_item WHERE status='CLAIMED'"),
        "resolved_items": count("SELECT count(*) c FROM review_item WHERE status='RESOLVED'"),
        "escalated_items": count("SELECT count(*) c FROM review_item WHERE status='ESCALATED'"),
        "total_items": count("SELECT count(*) c FROM review_item"),
        "by_reason": by_reason,
        "by_status": by_status,
        "advisory_only": True,
        "human_decision_required": True,
    }


def _esc(text):
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def render_queue_html(conn, *, status=None, tx_id=None, limit=200):
    """Self-contained advisory review dashboard for BAC staff. No external resources."""
    dash = queue_dashboard(conn)
    listing = list_queue(conn, status=status, tx_id=tx_id, limit=limit)
    items = listing["items"]
    details = []
    for item in items[:limit]:
        try:
            details.append(get_item(conn, item["id"]))
        except Exception:
            details.append(item)

    def badge(value, kind=""):
        cls = _esc(kind or str(value).lower())
        return f'<span class="badge {cls}">{_esc(value)}</span>'

    rows = []
    for it in details:
        v = it.get("verdict") or {}
        notes = v.get("notes") or []
        citations = v.get("rule_citations") or []
        evidence = v.get("evidence_spans") or []
        rec = "Review evidence/citations and record an advisory action; statutory decision requires authorized human sign-off."
        if v.get("decision") == "ABSTAIN":
            rec = "ABSTAIN: missing/uncertain evidence or validation issue. Verify source documents before any human determination."
        elif v.get("decision") == "FAIL":
            rec = "FAIL (advisory): deterministic checks found missing/invalid items. Verify against the physical/electronic record before any human determination."
        rows.append(f"""
        <section class="item">
          <h3>{badge(it.get('priority','NORMAL'), 'priority-' + str(it.get('priority','NORMAL')).lower())}
              {badge(it.get('status',''), 'status-' + str(it.get('status','')).lower())}
              {badge(it.get('reason',''))}
              <span class="tx">{_esc(it.get('tx_number') or it.get('tx_id'))}</span></h3>
          <p><strong>Item:</strong> {_esc(it.get('id'))} | <strong>Verdict:</strong> {_esc(it.get('verdict_id'))}
             | <strong>Decision:</strong> {badge(v.get('decision','?'))}
             | <strong>Confidence:</strong> {_esc(v.get('confidence'))}
             | <strong>Mode/stage:</strong> {_esc(it.get('tx_mode'))} / {_esc(it.get('tx_stage'))}</p>
          <p><strong>Missing:</strong> {_esc(v.get('missing_documents', it.get('missing_documents', [])))}
             | <strong>Invalid:</strong> {_esc(v.get('invalid_documents', it.get('invalid_documents', [])))}</p>
          <p class="rec"><strong>Suggested next step (advisory):</strong> {_esc(rec)}</p>
          <details><summary>Notes ({len(notes)})</summary><pre>{_esc(canonical(notes))}</pre></details>
          <details><summary>Rule citations ({len(citations)})</summary><pre>{_esc(canonical(citations))}</pre></details>
          <details><summary>Evidence spans ({len(evidence)})</summary><pre>{_esc(canonical(evidence))}</pre></details>
          <details><summary>Review actions ({len(it.get('actions') or [])})</summary><pre>{_esc(canonical(it.get('actions') or []))}</pre></details>
        </section>""")

    body = "\n".join(rows) or '<p class="empty">No review items match the current filter.</p>'
    filters = f"status={_esc(status)} tx_id={_esc(tx_id)} limit={int(limit)}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ProcEMP Advisory Review Queue</title>
  <style>
    :root{{--bg:#f7f8fa;--card:#fff;--ink:#17202a;--muted:#5b6775;--line:#d9e1ea;--warn:#8a4b00;--bad:#8f1d1d;--ok:#1f5f3b;--info:#1f4f8f}}
    body{{font-family:Segoe UI,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}}
    header{{padding:20px 24px;background:#fff;border-bottom:1px solid var(--line)}}
    h1,h2,h3{{margin:0 0 8px 0}} .muted{{color:var(--muted)}}
    .notice{{margin-top:10px;padding:12px 14px;border:1px solid #f0d9a8;background:#fff8e8;color:var(--warn);border-radius:8px}}
    main{{padding:20px 24px}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}}
    .card,.item{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
    .num{{font-size:28px;font-weight:700}} .item{{margin-bottom:12px}} .tx{{color:var(--muted);font-weight:normal;font-size:14px}}
    .badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line);margin-right:6px;background:#f2f5f8}}
    .abstain,.status-open,.priority-high,.priority-critical,.fail{{color:var(--bad);border-color:#efcaca;background:#fff3f3}}
    .pass,.status-resolved{{color:var(--ok);border-color:#cde6d8;background:#f2fbf6}}
    .status-claimed,.status-escalated{{color:var(--info);border-color:#cfe0f5;background:#f3f8ff}}
    pre{{white-space:pre-wrap;word-break:break-word;background:#fbfcfd;border:1px solid var(--line);padding:10px;border-radius:8px}}
    details{{margin-top:8px}} summary{{cursor:pointer;color:var(--info)}} .rec{{margin:8px 0}} .empty{{color:var(--muted)}}
  </style>
</head>
<body>
  <header>
    <h1>ProcEMP Advisory Review Queue</h1>
    <div class="muted">Generated: {_esc(now())} | Filters: {filters} | Self-contained HTML (no external resources)</div>
    <div class="notice"><strong>Advisory only.</strong> AI suggestions/flags in this queue do <strong>not</strong> make statutory procurement decisions.
    BAC/TWG/HOPE/Inspection/Accounting/authorized bodies must review evidence and sign/record official determinations through the governed workflow.</div>
  </header>
  <main>
    <h2>Summary</h2>
    <div class="cards">
      <div class="card"><div class="muted">Open</div><div class="num">{dash['open_items']}</div></div>
      <div class="card"><div class="muted">Claimed</div><div class="num">{dash['claimed_items']}</div></div>
      <div class="card"><div class="muted">Escalated</div><div class="num">{dash['escalated_items']}</div></div>
      <div class="card"><div class="muted">Resolved</div><div class="num">{dash['resolved_items']}</div></div>
      <div class="card"><div class="muted">Total</div><div class="num">{dash['total_items']}</div></div>
    </div>
    <h2>Queue Items</h2>
    {body}
  </main>
</body>
</html>"""


def export_queue_dashboard(conn, path, *, status=None, tx_id=None, limit=200):
    html = render_queue_html(conn, status=status, tx_id=tx_id, limit=limit)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


def _fmt_item(item):
    return (
        f"[{item['priority']}] {item['id']} tx={item.get('tx_number') or item['tx_id']} "
        f"decision={item.get('decision')} status={item['status']} reason={item['reason']} "
        f"missing={item.get('missing_documents', [])} invalid={item.get('invalid_documents', [])}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ProcEMP advisory review queue (BAC decision support; not a decision maker)."
    )
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "procurement.db"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dashboard")
    p = sub.add_parser("export")
    p.add_argument("--out", required=True, help="Output HTML path")
    p.add_argument("--status")
    p.add_argument("--tx-id")
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("list")
    p.add_argument("--status")
    p.add_argument("--tx-id")
    p.add_argument("--assigned-to")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("show")
    p.add_argument("item_id")
    p = sub.add_parser("enqueue")
    p.add_argument("--tx-id", required=True)
    p.add_argument("--verdict-id", required=True)
    p.add_argument("--created-by", required=True)
    p.add_argument("--reason")
    p.add_argument("--priority")
    p = sub.add_parser("claim")
    p.add_argument("item_id")
    p.add_argument("--actor-id", required=True)
    p.add_argument("--actor-role", default="BAC_MEMBER")
    p.add_argument("--note", default="")
    p = sub.add_parser("recommend")
    p.add_argument("item_id")
    p.add_argument("--actor-id", required=True)
    p.add_argument("--actor-role", default="BAC_MEMBER")
    p.add_argument("--recommendation", required=True, choices=sorted(ADVISORY_RECOMMENDATIONS))
    p.add_argument("--note", default="")
    p = sub.add_parser("override")
    p.add_argument("item_id")
    p.add_argument("--field-path", required=True)
    p.add_argument("--ai-value", required=True, help="JSON literal")
    p.add_argument("--correct-value", required=True, help="JSON literal")
    p.add_argument("--reason", required=True)
    p.add_argument("--submitted-by", required=True)
    p.add_argument("--actor-role", default="BAC_STAFF")
    p = sub.add_parser("adjudicate")
    p.add_argument("correction_id")
    p.add_argument("--decision", required=True, choices=["ACCEPTED", "REJECTED"])
    p.add_argument("--adjudicator", required=True)
    p.add_argument("--actor-role", default="SECOND_REVIEWER",
                   choices=ADJUDICATE_ROLES,
                   help="Adjudicator role (SoD still applies)")
    p.add_argument("--note", default="")
    p = sub.add_parser("resolve")
    p.add_argument("item_id")
    p.add_argument("--actor-id", required=True)
    p.add_argument("--actor-role", default="BAC_MEMBER")
    p.add_argument("--resolution", required=True)
    p.add_argument("--note", default="")

    args = parser.parse_args(argv)
    if args.cmd == "enqueue" and not os.path.exists(args.db):
        raise SystemExit(f"database not found: {args.db}")
    conn = init_db(args.db, seed=not os.path.exists(args.db)) if args.cmd == "dashboard" and not os.path.exists(args.db) else None
    if conn is None:
        if not os.path.exists(args.db):
            raise SystemExit(f"database not found: {args.db}")
        conn = init_db(args.db, seed=False)
    try:
        if args.cmd == "dashboard":
            out = queue_dashboard(conn)
        elif args.cmd == "export":
            path = export_queue_dashboard(conn, args.out, status=args.status,
                                          tx_id=args.tx_id, limit=args.limit)
            out = {"exported": path, "advisory_only": True}
        elif args.cmd == "list":
            out = list_queue(conn, status=args.status, tx_id=args.tx_id,
                             assigned_to=args.assigned_to, limit=args.limit)
            for item in out["items"]:
                print(_fmt_item(item))
            print(f"-- {out['count']} item(s), advisory_only={out['advisory']}")
            return 0
        elif args.cmd == "show":
            out = get_item(conn, args.item_id)
        elif args.cmd == "enqueue":
            out = enqueue_verdict(conn, args.tx_id, args.verdict_id,
                                  created_by=args.created_by,
                                  reason=args.reason, priority=args.priority)
        elif args.cmd == "claim":
            out = claim_item(conn, args.item_id, args.actor_id,
                             actor_role=args.actor_role, note=args.note)
        elif args.cmd == "recommend":
            out = recommend(conn, args.item_id, args.actor_id, args.actor_role,
                            args.recommendation, note=args.note)
        elif args.cmd == "override":
            out = submit_override_correction(
                conn, args.item_id,
                field_path=args.field_path,
                ai_value=json.loads(args.ai_value),
                correct_value=json.loads(args.correct_value),
                reason=args.reason,
                submitted_by=args.submitted_by,
                actor_role=args.actor_role,
            )
        elif args.cmd == "adjudicate":
            out = adjudicate_override(conn, args.correction_id, args.decision,
                                      args.adjudicator, args.note,
                                      actor_role=args.actor_role)
        elif args.cmd == "resolve":
            out = resolve_item(conn, args.item_id, args.actor_id, args.actor_role,
                               args.resolution, note=args.note)
        else:
            raise SystemExit(f"unknown command: {args.cmd}")
        print(canonical(out))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())