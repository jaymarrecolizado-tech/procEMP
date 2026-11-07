"""Step 9: contract implementation monitoring (Stage 7 of the workflow).

AI tracks and FLAGS; the project manager / TWG / HOPE make implementation
decisions. In particular, a liquidated-damages figure here is an ADVISORY
ESTIMATE from the approved contract's own rate — never a determination, and
never derived from a model's idea of the law. If the rate is not recorded, the
system reports NOT_CONFIGURED instead of guessing.
"""

from __future__ import annotations

import html
import os
from datetime import date, timedelta

from core import CoreError, STAGES, append_event, get_tx, now, uid

VALID_VO_DECISIONS = ("APPROVED", "REJECTED")
DUE_SOON_DAYS = 7


def _d(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _require_stage(tx, min_stage):
    if STAGES.index(tx["stage"]) < STAGES.index(min_stage):
        raise CoreError(
            f"transaction is at stage {tx['stage']}; {min_stage} or later is required"
        )


# ---------------------------------------------------------------- setup

def register_contract(conn, tx_id, *, contract_amount, start_date, completion_date,
                      approved_by, ld_rate_per_day=None, ld_cap_ratio=None):
    """Contract terms come from the signed contract and a named human approver."""
    tx = get_tx(conn, tx_id)
    _require_stage(tx, "CONTRACT")
    if not approved_by:
        raise CoreError("approved_by (a named HOPE / authorized official) is required")
    if contract_amount is None or float(contract_amount) <= 0:
        raise CoreError("contract_amount must be positive")
    if _d(completion_date) < _d(start_date):
        raise CoreError("completion_date cannot precede start_date")
    conn.execute(
        "INSERT OR REPLACE INTO contract_terms "
        "(tx_id, contract_amount, start_date, completion_date, ld_rate_per_day, "
        " ld_cap_ratio, approved_by, approved_at) VALUES (?,?,?,?,?,?,?,?)",
        (tx_id, float(contract_amount), str(start_date), str(completion_date),
         ld_rate_per_day, ld_cap_ratio, approved_by, now()),
    )
    append_event(conn, tx_id, "CONTRACT_TERMS_REGISTERED", approved_by, "HOPE", {
        "contract_amount": float(contract_amount),
        "start_date": str(start_date), "completion_date": str(completion_date),
        "ld_rate_per_day": ld_rate_per_day, "ld_cap_ratio": ld_cap_ratio,
    })
    return {"tx_id": tx_id, "completion_date": str(completion_date)}


def register_milestones(conn, tx_id, milestones, approved_by):
    if not approved_by:
        raise CoreError("approved_by is required for the milestone schedule")
    if not milestones:
        raise CoreError("milestone list cannot be empty")
    terms = conn.execute("SELECT 1 FROM contract_terms WHERE tx_id=?", (tx_id,)).fetchone()
    if not terms:
        raise CoreError("register_contract() must run before the milestone schedule")
    conn.execute("DELETE FROM milestone WHERE tx_id=?", (tx_id,))
    created = []
    for i, m in enumerate(milestones, start=1):
        if not m.get("name") or not m.get("due_date"):
            raise CoreError(f"milestone {i} needs name and due_date")
        mid = uid()
        conn.execute(
            "INSERT INTO milestone (id, tx_id, sequence, name, due_date, amount, status) "
            "VALUES (?,?,?,?,?,?,'PENDING')",
            (mid, tx_id, i, m["name"], str(m["due_date"]), m.get("amount")),
        )
        created.append(mid)
    append_event(conn, tx_id, "MILESTONES_REGISTERED", approved_by, "PROJECT_MANAGER", {
        "count": len(created),
        "schedule": [{"name": m["name"], "due_date": str(m["due_date"])}
                     for m in milestones],
    })
    return created


def record_milestone_completion(conn, milestone_id, completed_on, evidence_doc_id,
                                verified_by):
    """Human-verified progress. AI cannot mark a milestone complete."""
    if not verified_by:
        raise CoreError("verified_by (a named inspector / project manager) is required")
    row = conn.execute("SELECT * FROM milestone WHERE id=?", (milestone_id,)).fetchone()
    if not row:
        raise CoreError("unknown milestone")
    if row["status"] in ("COMPLETED", "DELAYED"):
        raise CoreError("milestone already recorded")
    late = _d(completed_on) > _d(row["due_date"])
    days_late = max(0, (_d(completed_on) - _d(row["due_date"])).days)
    status = "DELAYED" if late else "COMPLETED"
    conn.execute(
        "UPDATE milestone SET status=?, completed_at=?, evidence_doc_id=?, verified_by=? "
        "WHERE id=?",
        (status, str(completed_on), evidence_doc_id, verified_by, milestone_id),
    )
    append_event(conn, row["tx_id"], "MILESTONE_COMPLETED", verified_by, "PROJECT_MANAGER", {
        "milestone_id": milestone_id, "name": row["name"],
        "due_date": row["due_date"], "completed_on": str(completed_on),
        "status": status, "days_late": days_late,
        "evidence_doc_id": evidence_doc_id,
    })
    if late:
        append_event(conn, row["tx_id"], "DELAY_FLAGGED", "monitoring", "RULES_ENGINE", {
            "milestone_id": milestone_id, "name": row["name"],
            "days_late": days_late, "action": "flag only; project manager decides",
        })
    return {"milestone_id": milestone_id, "status": status, "days_late": days_late}


# ---------------------------------------------------------------- variation orders

def raise_variation_order(conn, tx_id, *, reference, description, reason,
                          requested_by, requested_amount=None):
    tx = get_tx(conn, tx_id)
    _require_stage(tx, "CONTRACT")
    if not reference or not description or not reason:
        raise CoreError("reference, description and reason are required")
    vo_id = uid()
    conn.execute(
        "INSERT INTO variation_order (id, tx_id, reference, description, "
        "requested_amount, reason, status, requested_by, created_at) "
        "VALUES (?,?,?,?,?,?,'PENDING',?,?)",
        (vo_id, tx_id, reference, description, requested_amount, reason,
         requested_by, now()),
    )
    append_event(conn, tx_id, "VARIATION_ORDER_RAISED", requested_by, "PROJECT_MANAGER", {
        "vo_id": vo_id, "reference": reference,
        "requested_amount": requested_amount,
        "status": "PENDING", "note": "requires human review; AI cannot approve",
    })
    return {"vo_id": vo_id, "status": "PENDING"}


def review_variation_order(conn, vo_id, decision, reviewer):
    if decision not in VALID_VO_DECISIONS:
        raise CoreError(f"decision must be one of {VALID_VO_DECISIONS}")
    if not reviewer:
        raise CoreError("reviewer (a named approving authority) is required")
    row = conn.execute("SELECT * FROM variation_order WHERE id=?", (vo_id,)).fetchone()
    if not row:
        raise CoreError("unknown variation order")
    if row["status"] != "PENDING":
        raise CoreError(f"variation order is already {row['status']}")
    if reviewer == row["requested_by"]:
        raise CoreError("segregation of duties: the requester cannot approve their own VO")
    conn.execute(
        "UPDATE variation_order SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
        (decision, reviewer, now(), vo_id),
    )
    append_event(conn, row["tx_id"], "VARIATION_ORDER_REVIEWED", reviewer,
                 "APPROVING_AUTHORITY", {
                     "vo_id": vo_id, "reference": row["reference"],
                     "decision": decision, "requested_by": row["requested_by"],
                 })
    return {"vo_id": vo_id, "status": decision, "reviewed_by": reviewer}


# ---------------------------------------------------------------- status + alerts

def compute_status(conn, tx_id, as_of=None):
    as_of = _d(as_of) if as_of else date.today()
    terms = conn.execute("SELECT * FROM contract_terms WHERE tx_id=?", (tx_id,)).fetchone()
    if not terms:
        return {"tx_id": tx_id, "monitored": False,
                "reason": "no approved contract terms registered"}
    milestones = [dict(r) for r in conn.execute(
        "SELECT * FROM milestone WHERE tx_id=? ORDER BY sequence", (tx_id,)).fetchall()]
    vos = [dict(r) for r in conn.execute(
        "SELECT * FROM variation_order WHERE tx_id=? ORDER BY created_at", (tx_id,)).fetchall()]
    done = [m for m in milestones if m["status"] in ("COMPLETED", "DELAYED")]
    total_amount = sum(m["amount"] or 0 for m in milestones)
    done_amount = sum(m["amount"] or 0 for m in done)
    pct_count = round(100 * len(done) / len(milestones), 1) if milestones else 0.0
    pct_amount = (round(100 * done_amount / total_amount, 1)
                  if total_amount else None)
    completion = _d(terms["completion_date"])
    days_late_contract = max(0, (as_of - completion).days)
    fully_complete = bool(milestones) and len(done) == len(milestones)
    overdue = [m for m in milestones
               if m["status"] == "PENDING" and _d(m["due_date"]) < as_of]
    due_soon = [m for m in milestones
                if m["status"] == "PENDING"
                and as_of <= _d(m["due_date"]) <= as_of + timedelta(days=DUE_SOON_DAYS)]

    ld = {
        "status": "NOT_CONFIGURED", "amount": None,
        "reason": "LD rate from the approved contract is not recorded; "
                  "the HOPE/TWG must determine any liquidated damages",
    }
    if terms["ld_rate_per_day"] is not None and days_late_contract and not fully_complete:
        raw = terms["contract_amount"] * terms["ld_rate_per_day"] * days_late_contract
        cap = terms["contract_amount"] * (terms["ld_cap_ratio"] or 1.0)
        ld = {
            "status": "ADVISORY_ESTIMATE_ONLY",
            "amount": round(min(raw, cap), 2),
            "days_late": days_late_contract,
            "rate_per_day": terms["ld_rate_per_day"],
            "cap_ratio": terms["ld_cap_ratio"],
            "formula": "min(contract_amount * ld_rate_per_day * days_late, contract_amount * cap_ratio)",
            "note": "Advisory estimate from the approved contract terms only; "
                    "NOT a determination. Project manager / HOPE decides.",
        }

    alerts = []
    for m in overdue:
        alerts.append({"code": "MILESTONE_OVERDUE", "severity": "critical",
                       "tx_id": tx_id, "message": f"Milestone overdue: {m['name']} "
                                                  f"(due {m['due_date']})"})
    for m in due_soon:
        alerts.append({"code": "MILESTONE_DUE_SOON", "severity": "warning",
                       "tx_id": tx_id, "message": f"Due soon: {m['name']} "
                                                  f"({m['due_date']})"})
    if days_late_contract and not fully_complete:
        alerts.append({"code": "CONTRACT_OVERRUN", "severity": "critical",
                       "tx_id": tx_id,
                       "message": f"{days_late_contract} day(s) past completion date"})
        alerts.append({"code": "LD_TRIGGER", "severity": "warning", "tx_id": tx_id,
                       "message": "Liquidated damages may apply — human review required"})
    if ld["status"] == "NOT_CONFIGURED":
        alerts.append({"code": "LD_RATE_NOT_CONFIGURED", "severity": "info",
                       "tx_id": tx_id,
                       "message": "LD rate missing from contract terms; no estimate produced"})
    for vo in vos:
        if vo["status"] == "PENDING":
            alerts.append({"code": "VARIATION_ORDER_PENDING_REVIEW",
                           "severity": "warning", "tx_id": tx_id,
                           "message": f"VO {vo['reference']} awaiting human decision"})

    return {
        "tx_id": tx_id, "monitored": True, "as_of": as_of.isoformat(),
        "contract_amount": terms["contract_amount"],
        "completion_date": terms["completion_date"],
        "days_late_contract": days_late_contract,
        "fully_complete": fully_complete,
        "milestones_total": len(milestones), "milestones_done": len(done),
        "percent_complete_count": pct_count, "percent_complete_amount": pct_amount,
        "overdue_milestones": [m["name"] for m in overdue],
        "due_soon_milestones": [m["name"] for m in due_soon],
        "liquidated_damages": ld,
        "variation_orders": [{"reference": v["reference"], "status": v["status"]}
                             for v in vos],
        "alerts": alerts,
        "milestones": milestones,
        "decision_authority": "project manager / TWG / HOPE — AI flags only",
    }


def dashboard(conn, as_of=None):
    rows = conn.execute("SELECT tx_id FROM contract_terms ORDER BY tx_id").fetchall()
    statuses = [compute_status(conn, r["tx_id"], as_of) for r in rows]
    statuses = [s for s in statuses if s.get("monitored")]
    alerts = [a for s in statuses for a in s["alerts"]]
    open_vos = [v for s in statuses for v in s["variation_orders"]
                if v["status"] == "PENDING"]
    return {
        "as_of": (as_of or date.today().isoformat()),
        "contracts_tracked": len(statuses),
        "total_contract_value": round(sum(s["contract_amount"] for s in statuses), 2),
        "at_risk_contracts": sum(1 for s in statuses
                                 if any(a["severity"] == "critical"
                                        for a in s["alerts"])),
        "fully_complete": sum(1 for s in statuses if s["fully_complete"]),
        "open_variation_orders": len(open_vos),
        "alerts_by_severity": {
            "critical": sum(1 for a in alerts if a["severity"] == "critical"),
            "warning": sum(1 for a in alerts if a["severity"] == "warning"),
            "info": sum(1 for a in alerts if a["severity"] == "info"),
        },
        "alerts": alerts,
        "contracts": statuses,
        "note": "Advisory monitoring only. All implementation decisions are human.",
    }


# ---------------------------------------------------------------- HTML view

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;padding:32px;background:#f7f8fa;color:#1c2024;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:14px;line-height:1.55}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
h2{font-size:15px;font-weight:600;margin:28px 0 12px}
.sub{color:#6b7280;margin:0 0 24px;font-size:13px}
.kpis{display:flex;gap:12px;flex-wrap:wrap}
.kpi{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px 18px;min-width:150px}
.kpi .v{font-size:22px;font-weight:600;margin-top:4px}
.kpi .l{font-size:12px;color:#6b7280}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e6ea;border-radius:10px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #eef0f3;font-size:13px;vertical-align:middle}
th{background:#f1f3f6;font-weight:600;color:#3c424a}
tr:last-child td{border-bottom:0}
.bar{background:#eceff3;border-radius:6px;height:8px;width:120px;overflow:hidden}
.bar > div{background:#2f6feb;height:100%}
.pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.critical{background:#fdecec;color:#a32d2d}
.warning{background:#fdf3e0;color:#8a5a06}
.info{background:#e9f1fb;color:#185fa5}
.ok{background:#e7f5ee;color:#0f6e56}
.note{background:#fff;border:1px solid #e3e6ea;border-left:3px solid #2f6feb;
 border-radius:8px;padding:12px 16px;margin-top:24px;color:#4a5058;font-size:12px}
code{background:#f1f3f6;padding:1px 5px;border-radius:4px;font-size:12px}
"""


def render_dashboard_html(conn, as_of=None):
    data = dashboard(conn, as_of)
    e = html.escape

    def kpi(value, label):
        return f'<div class="kpi"><div class="l">{e(label)}</div><div class="v">{e(str(value))}</div></div>'

    rows = []
    for s in data["contracts"]:
        pct = s["percent_complete_count"]
        severity = ("critical" if any(a["severity"] == "critical" for a in s["alerts"])
                    else "ok")
        state = "At risk" if severity == "critical" else ("Complete" if s["fully_complete"] else "On track")
        rows.append(
            "<tr>"
            f"<td><code>{e(s['tx_id'][:8])}</code></td>"
            f"<td>PHP {s['contract_amount']:,.2f}</td>"
            f"<td>{e(s['completion_date'])}</td>"
            f"<td><div class=\"bar\"><div style=\"width:{pct}%\"></div></div> {pct}%</td>"
            f"<td>{s['milestones_done']}/{s['milestones_total']}</td>"
            f"<td><span class=\"pill {severity}\">{e(state)}</span></td>"
            "</tr>"
        )
    contract_table = (
        "<table><thead><tr><th>Transaction</th><th>Contract value</th>"
        "<th>Completion</th><th>Progress</th><th>Milestones</th><th>Status</th>"
        "</tr></thead><tbody>" + ("".join(rows) or
        "<tr><td colspan='6'>No contracts registered yet.</td></tr>") + "</tbody></table>"
    )

    alert_rows = "".join(
        f'<tr><td><span class="pill {e(a["severity"])}">{e(a["severity"].upper())}</span></td>'
        f'<td><code>{e(a["code"])}</code></td><td>{e(a["message"])}</td></tr>'
        for a in data["alerts"]
    ) or "<tr><td colspan='3'>No alerts.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Procurement monitoring dashboard</title><style>{_CSS}</style></head>
<body>
<h1>Contract implementation monitoring</h1>
<p class="sub">Advisory dashboard &middot; as of {e(str(data['as_of']))} &middot; generated locally, no external services</p>
<div class="kpis">
{kpi(data['contracts_tracked'], 'Contracts tracked')}
{kpi(f"PHP {data['total_contract_value']:,.2f}", 'Total contract value')}
{kpi(data['at_risk_contracts'], 'At-risk contracts')}
{kpi(data['fully_complete'], 'Fully complete')}
{kpi(data['open_variation_orders'], 'Open variation orders')}
{kpi(data['alerts_by_severity']['critical'], 'Critical alerts')}
</div>
<h2>Contracts</h2>
{contract_table}
<h2>Alerts</h2>
<table><thead><tr><th>Severity</th><th>Code</th><th>Message</th></tr></thead>
<tbody>{alert_rows}</tbody></table>
<div class="note">
All figures are advisory. Liquidated damages, where shown, are an estimate from the
<strong>approved contract's own rate</strong> &mdash; never a determination. Milestone
completion is human-verified, and variation orders require a named approving authority.
Implementation decisions belong to the project manager, TWG and HOPE.
</div>
</body></html>"""


def export_dashboard(conn, out_path, as_of=None):
    content = render_dashboard_html(conn, as_of)
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return out_path
