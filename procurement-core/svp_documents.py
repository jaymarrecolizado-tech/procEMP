"""SVP document chain drafting — AI-assisted, deterministic values, PDF output.

Given transaction parameters (end user, purpose, items, dates, officials,
suppliers), drafts the Small Value Procurement document chain:

  Purchase Request -> Request for Quotation -> Minutes of Opening of Bids ->
  Abstract of Bids/Canvass -> BAC Resolution Recommending Award ->
  Purchase Order

Guardrails (mirroring the project's, see plan.md §4):
- All amounts are COMPUTED from item lines, never invented; document numbers
  come from the gap-free ledger numbering (core.issue_number).
- Supplier bid amounts are HUMAN-ENTERED canvass data — the caller supplies
  them; the model never invents prices, and validate_chain() re-checks
  lowest-calculated-responsive-bid logic before anything is rendered.
- validate_chain() fails closed: a words-vs-figures mismatch, fund-cluster
  drift, award above ABC, or broken date chain blocks PDF rendering
  (render_pdfs refuses invalid chains unless explicitly overridden, and then
  stamps every page with a violations banner).
- Every rendered page is watermarked "AI-ASSISTED DRAFT" — documents are
  drafts until named humans sign; they are never official GPPB form bodies
  (release controls live in forms.py).

  python svp_documents.py demo --out ./svp-output   # fictional end-to-end set
  python svp_documents.py words --amount 26100      # words helper
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta

from core import issue_number, now
import pdf_writer as pw

UNIT_CODE = "RO2"

DOC_ORDER = (
    "purchase_request", "request_for_quotation", "minutes_of_opening",
    "abstract_of_bids", "bac_resolution_award", "purchase_order")

DRAFT_BANNER = ("AI-ASSISTED DRAFT - ProcEMP prototype. Not an official form "
                "until approved and signed by the named humans.")


# ------------------------------------------------------------ amount words

_ONES = ["", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT",
         "NINE", "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN",
         "FIFTEEN", "SIXTEEN", "SEVENTEEN", "EIGHTEEN", "NINETEEN"]
_TENS = ["", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY",
         "EIGHTY", "NINETY"]
_SCALES = ["", " THOUSAND", " MILLION", " BILLION"]


def _three_digit_words(n):
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " HUNDRED")
        n %= 100
    if n >= 20:
        tens = _TENS[n // 10]
        if n % 10:
            tens += "-" + _ONES[n % 10]
        parts.append(tens)
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def amount_in_words(amount):
    """Philippine banking style: 'TWENTY-SIX THOUSAND ONE HUNDRED PESOS
    ONLY' / '... PESOS AND 25/100 (CENTAVOS) ONLY'. Deterministic; this
    helper exists because words-vs-figures mismatch is a common manual error
    (the prototype's sample corpus contains one)."""
    if amount is None or amount < 0 or amount > 999_999_999_999.99:
        raise ValueError(f"amount out of range: {amount!r}")
    pesos = int(amount)
    centavos = int(round((amount - pesos) * 100))
    if centavos == 100:  # rounding edge (e.g. 0.999)
        pesos += 1
        centavos = 0
    if pesos == 0 and centavos == 0:
        base = "ZERO PESOS"
    else:
        groups, n = [], pesos
        scale = 0
        while n:
            group = n % 1000
            if group:
                words = _three_digit_words(group) + _SCALES[scale]
                groups.append(words)
            n //= 1000
            scale += 1
        base = " ".join(reversed(groups)) + " PESOS"
    if centavos:
        base += f" AND {centavos:02d}/100 (CENTAVOS)"
    return base + " ONLY"


# ------------------------------------------------------------ item totals

def compute_items(items):
    """Recompute per-line totals and the grand total. Returns
    (computed_total, violations). Never trusts caller-supplied line totals."""
    violations, total = [], 0.0
    for i, item in enumerate(items or [], start=1):
        qty = item.get("quantity")
        cost = item.get("unit_cost")
        if not isinstance(qty, (int, float)) or not isinstance(cost, (int, float)):
            violations.append(f"item {i}: quantity/unit_cost must be numbers")
            continue
        if qty <= 0 or cost < 0:
            violations.append(f"item {i}: invalid quantity/unit_cost")
            continue
        line_total = round(qty * cost, 2)
        item["total_cost"] = line_total
        total = round(total + line_total, 2)
    return total, violations


# ------------------------------------------------------------ chain draft

def _fmt_money(v):
    return f"{v:,.2f}"


def _fmt_peso(v):
    return f"PHP {_fmt_money(v)}"


def draft_chain(conn, params):
    """Build the 6-document SVP chain from transaction parameters.

    Requires a ProcEMP DB connection for gap-free ledger numbering.
    Returns (docs, report) where each doc carries `data`, `render` spec and
    `file_hint`; report carries violations/notes from validate_chain().
    """
    mode = params.get("mode_of_procurement", "Small Value Procurement")
    year = int(params["deadline_date"][:4])
    pr_no = issue_number(conn, "PR", UNIT_CODE, year)
    rfq_no = issue_number(conn, "RFQ", UNIT_CODE, year)
    res_no = issue_number(conn, "RES", UNIT_CODE, year)
    po_no = issue_number(conn, "PO", UNIT_CODE, year)

    items = [dict(it) for it in params["items"]]
    total, item_violations = compute_items(items)
    purpose = params["purpose"]

    award = params.get("award") or {}
    award_supplier = award.get("supplier")
    suppliers = params.get("suppliers") or []
    po_items = [dict(it) for it in (award.get("items") or items)]
    po_total, po_item_violations = compute_items(po_items)

    fund_cluster = params.get("fund_cluster", "")
    header = params.get("header") or [
        "REPUBLIC OF THE PHILIPPINES",
        "DEPARTMENT OF INFORMATION AND COMMUNICATIONS TECHNOLOGY",
        "Regional Office 02",
    ]
    deadline_date = params["deadline_date"]
    deadline_time = params.get("deadline_time", "9:00 AM")
    opening_date = params.get("opening_date", deadline_date)
    opening_time = params.get("opening_time", "11:00 AM")
    delivery_date = params.get("delivery_date")
    resolved_date = params.get("resolved_date", opening_date)
    po_date = params.get("po_date", resolved_date)
    amount_words = params.get("amount_in_words") or amount_in_words(po_total)

    docs = {}

    # -------------------------------------------------------- 1. PR
    docs["purchase_request"] = {
        "file_hint": "01-purchase-request.pdf",
        "data": {
            "pr_number": pr_no, "pr_date": params["pr_date"],
            "office_section": params.get("office_section", ""),
            "responsibility_center_code": params.get("responsibility_center_code", ""),
            "fund_cluster": fund_cluster, "items": items,
            "total_amount": total, "purpose": purpose,
            "requested_by": params["requested_by"]["name"],
            "requested_by_designation": params["requested_by"]["designation"],
            "approved_by": params["approved_by"]["name"],
            "approved_by_designation": params["approved_by"]["designation"],
        },
        "render": {
            "title": "PURCHASE REQUEST",
            "header": header + ([params["header_address"]] if params.get("header_address") else []),
            "meta_rows": [
                ("Entity Name:", params.get("entity_name", ""), "Fund Cluster:", fund_cluster),
                ("Office/Section:", params.get("office_section", ""), "Purchase Request Number:", pr_no),
                ("Responsibility Center Code:", params.get("responsibility_center_code", ""), "Date:", params["pr_date"]),
            ],
            "table": {
                "headers": ["Stock/Property No.", "Unit", "Item Description", "Quantity", "Unit Cost", "Total Cost"],
                "col_right": [3, 4, 5],
                "rows": [[
                    it.get("stock_no", ""), it.get("unit", ""),
                    it.get("description", ""), str(it.get("quantity", "")),
                    _fmt_peso(it.get("unit_cost", 0)), _fmt_peso(it.get("total_cost", 0)),
                ] for it in items] + [["", "", "", "", "TOTAL", _fmt_peso(total)]],
            },
            "purpose": purpose,
            "signatories": [
                (params["requested_by"]["name"], params["requested_by"]["designation"]),
                (params["approved_by"]["name"], params["approved_by"]["designation"]),
            ],
        },
    }

    # -------------------------------------------------------- 2. RFQ
    docs["request_for_quotation"] = {
        "file_hint": "02-request-for-quotation.pdf",
        "data": {
            "rfq_number": rfq_no, "pr_number_ref": pr_no,
            "project_title": purpose, "abc": total,
            "end_user": params.get("end_user", ""), "mode_of_procurement": mode,
            "delivery_period": delivery_date, "submission_deadline_date": deadline_date,
            "submission_deadline_time": deadline_time,
            "bac_secretariat_head": params["secretariat_head"]["name"],
            "contact_email": params.get("contact_email", ""),
            "required_documents": params.get("required_documents", []),
            "items": [
                {"item_no": i, "unit": it.get("unit", ""), "quantity": it.get("quantity"),
                 "description": it.get("description", ""), "abc_per_unit": it.get("unit_cost")}
                for i, it in enumerate(items, start=1)],
        },
        "render": {
            "title": "REQUEST FOR QUOTATION",
            "header": header + ([params["header_address"]] if params.get("header_address") else []),
            "meta_rows": [
                ("Procurement Project Title/Purpose:", purpose, "", ""),
                ("Approved Budget for the Contract (ABC):", _fmt_money(total), "End User:", params.get("end_user", "")),
                ("Purchase Request No.:", pr_no, "Mode of Procurement:", mode),
                ("Request for Quotation No.:", rfq_no, "Delivery Period (CD):", delivery_date or ""),
            ],
            "notice": (
                f"Please quote your lowest price on the item/s listed below, stating the shortest "
                f"time of delivery and submit your quotation duly signed by you or your authorized "
                f"representative not later than {deadline_date} at {deadline_time}, addressed to:"),
            "notice_person": [
                params["secretariat_head"]["name"],
                "Head, BAC Secretariat",
                params.get("end_user", ""),
                "Email Address: " + params.get("contact_email", ""),
            ],
            "table": {
                "headers": ["Item No.", "Unit", "Qty", "Item Description (Minimum Technical Specifications)", "ABC per unit"],
                "col_right": [0, 2, 4],
                "rows": [[
                    str(i), it.get("unit", ""), str(it.get("quantity")),
                    it.get("description", ""), _fmt_money(it.get("unit_cost", 0)),
                ] for i, it in enumerate(items, start=1)] + [["", "", "", "**nothing follows**", ""]],
            },
            "signatories": [
                (params["bac_chairperson"]["name"], "BAC Chairperson"),
                ("", "Signature over printed name of canvasser"),
            ],
        },
    }

    # -------------------------------------------------------- 3. Minutes
    docs["minutes_of_opening"] = {
        "file_hint": "03-minutes-of-opening.pdf",
        "data": {
            "rfq_number_ref": rfq_no, "opening_date": opening_date,
            "opening_time": opening_time,
            "attendees": params.get("bac_members", []),
            "supplier_bids": suppliers, "items": items,
            "lcrb_supplier": award_supplier or "",
        },
        "render": {
            "title": "MINUTES OF OPENING OF BID PRICE QUOTATION",
            "header": header + ([params["header_address"]] if params.get("header_address") else []),
            "meta_rows": [
                ("TIME & DATE:", f"{opening_time}  {opening_date}", "", ""),
                ("Purchase Request No.:", pr_no, "Request for Quotation No.:", rfq_no),
                ("Mode of Procurement:", mode, "Place of Delivery:", params.get("place_of_delivery", "")),
            ],
            "attendance": [(m.get("name", ""), m.get("role", ""))
                           for m in params.get("bac_members", [])],
            "supplier_totals": [(s.get("supplier", ""), _fmt_peso(s.get("bid_amount", 0)))
                                for s in suppliers],
            "table": {
                "headers": ["Item No.", "Unit", "Qty", "Item Description"] + [s.get("supplier", "") for s in suppliers],
                "col_right": [0, 2],
                "rows": [[
                    str(i), it.get("unit", ""), str(it.get("quantity")),
                    it.get("description", ""),
                ] + ["quoted" for _ in suppliers]
                    for i, it in enumerate(items, start=1)],
            },
            "signatories": [
                (params["secretariat_head"]["name"], "BAC Secretariat"),
                (params["bac_chairperson"]["name"], "BAC Chairperson"),
            ],
        },
    }

    # -------------------------------------------------------- 4. Abstract
    docs["abstract_of_bids"] = {
        "file_hint": "04-abstract-of-bids.pdf",
        "data": {
            "rfq_number_ref": rfq_no, "pr_number_ref": pr_no, "abc": total,
            "end_user": params.get("end_user", ""), "mode_of_procurement": mode,
            "delivery_period": delivery_date, "canvass_date": opening_date,
            "place_of_opening": params.get("place_of_delivery", ""),
            "suppliers": suppliers, "items": items,
            "lcrb_supplier": award_supplier or "",
            "awarded_total": (award.get("bid_amount") if award else None) or po_total,
        },
        "render": {
            "title": "ABSTRACT OF AWARDS / CANVASS",
            "header": header + ([params["header_address"]] if params.get("header_address") else []),
            "meta_rows": [
                ("Procurement Project Title/Purpose:", purpose, "", ""),
                ("Approved Budget for the Contract (ABC):", _fmt_peso(total), "End User:", params.get("end_user", "")),
                ("Purchase Request No.:", pr_no, "Mode of Procurement:", mode),
                ("Request for Quotation No.:", rfq_no, "Delivery Period (CD):", delivery_date or ""),
            ],
            "narrative": (
                f"Abstract of canvass on prices for furnishing and delivering item/s listed below. "
                f"Canvass opened at {params.get('place_of_delivery', '')} on {opening_date} with the "
                f"presence of the Chairman and Members."),
            "table": {
                "headers": ["Supplier"] + [s.get("supplier", "") for s in suppliers] + ["Lowest Calculated Responsive Bid"],
                "col_right": list(range(1, len(suppliers) + 2)),
                "rows": [["TOTAL BID"] + [_fmt_peso(s.get("bid_amount", 0)) for s in suppliers]
                         + [_fmt_peso(award.get("bid_amount", 0)) if award else ""]],
            },
            "purpose": purpose,
            "signatories": [
                (m.get("name", ""), m.get("role", ""))
                for m in params.get("bac_members", [])
            ] + [(params["bac_chairperson"]["name"], "BAC Chairperson")],
        },
    }

    # -------------------------------------------------------- 5. Resolution
    docs["bac_resolution_award"] = {
        "file_hint": "05-bac-resolution-award.pdf",
        "data": {
            "resolution_number": res_no, "project_title": purpose,
            "pr_number_ref": pr_no, "abc": total, "mode_of_procurement": mode,
            "opening_date": opening_date, "awarded_supplier": award_supplier or "",
            "awarded_amount": po_total, "award_basis": "LCRB",
            "resolved_date": resolved_date,
            "signatories": params.get("bac_members", []),
            "approved_by": params["approved_by"]["name"],
        },
        "render": {
            "title": "BAC RESOLUTION RECOMMENDING AWARD",
            "header": header + ([params["header_address"]] if params.get("header_address") else []),
            "title_line2": purpose,
            "meta_rows": [
                ("BAC Resolution No.:", res_no, "", ""),
            ],
            "whereas": [
                (f"WHEREAS, the {params.get('entity_name', 'agency')} conducted an invitation to "
                 f"submit quotations for {purpose} with PR No. {pr_no}, having an approved budget "
                 f"contract of { _fmt_peso(total)} under {mode} as mode of procurement, evaluated "
                 f"and awarded on a LOT basis;"),
                (f"WHEREAS, an Opening of Quotations was conducted by the Bids and Awards Committee "
                 f"(BAC) on {opening_date}, and the Abstract of Quotations showed that among the "
                 f"suppliers who submitted quotations, {award_supplier or '[AWARDED SUPPLIER]'} was "
                 f"found responsive to the requirements under the Request for Quotation (RFQ) with "
                 f"the lowest calculated responsive bid of {_fmt_peso(po_total)};"),
                (f"NOW THEREFORE, We, the members of the Bids and Awards Committee (BAC), resolve "
                 f"as it is hereby resolved to recommend the award of the contract for the "
                 f"procurement of said goods through {mode} in the total amount of "
                 f"{_fmt_peso(po_total)} to {award_supplier or '[AWARDED SUPPLIER]'}, which "
                 f"submitted the Lowest Calculated Responsive Bid."),
            ],
            "resolved_line": (f"Resolved, at {params.get('end_user', '')}, "
                              f"this {resolved_date}."),
            "signatories": [
                (m.get("name", ""), m.get("role", ""))
                for m in params.get("bac_members", [])
            ] + [(params["approved_by"]["name"],
                  params["approved_by"]["designation"] + " (HoPE)")],
        },
    }

    # -------------------------------------------------------- 6. PO
    docs["purchase_order"] = {
        "file_hint": "06-purchase-order.pdf",
        "data": {
            "po_number": po_no, "po_date": po_date,
            "supplier": award_supplier or "", "supplier_address": award.get("address", ""),
            "supplier_tin": award.get("tin", ""), "mode_of_procurement": mode,
            "place_of_delivery": params.get("place_of_delivery", ""),
            "delivery_date": delivery_date,
            "payment_term": params.get("payment_term", ""),
            "items": po_items, "total_amount": po_total,
            "amount_in_words": amount_words, "fund_cluster": fund_cluster,
            "accountant": params.get("accountant", ""),
            "ors_burs_number": award.get("ors_burs_number", ""),
        },
        "render": {
            "title": "PURCHASE ORDER",
            "header": header + ([params["header_address"]] if params.get("header_address") else []),
            "meta_rows": [
                ("Supplier:", award_supplier or "", "PO No.:", po_no),
                ("Address:", award.get("address", ""), "Date:", po_date),
                ("TIN:", award.get("tin", ""), "Mode of Procurement:", mode),
                ("Place of Delivery:", params.get("place_of_delivery", ""), "Delivery Term:", award.get("delivery_term", "")),
                ("Date of Delivery:", delivery_date or "", "Payment Term:", params.get("payment_term", "")),
            ],
            "table": {
                "headers": ["Stock/Property No.", "Unit", "Item Description", "Qty", "Unit Cost", "Total Cost"],
                "col_right": [3, 4, 5],
                "rows": [[
                    it.get("stock_no", ""), it.get("unit", ""),
                    it.get("description", ""), str(it.get("quantity", "")),
                    _fmt_peso(it.get("unit_cost", 0)), _fmt_peso(it.get("total_cost", 0)),
                ] for it in po_items] + [["", "", "", "", "TOTAL", _fmt_peso(po_total)]],
            },
            "purpose": purpose,
            "words_row": (f"(Total Amount in Words) {amount_words}", _fmt_peso(po_total)),
            "signatories": [
                (award.get("representative", ""), "Signature over Printed Name of Supplier"),
                (params["approved_by"]["name"], params["approved_by"]["designation"]),
            ],
            "extra_blocks": [
                ("Fund Cluster:", fund_cluster),
                ("ORS/BURS No.:", award.get("ors_burs_number", "")),
                ("Accountant:", params.get("accountant", "")),
            ],
        },
    }

    report = validate_chain(docs)
    report["violations"] = item_violations + po_item_violations + report["violations"]
    report["numbering"] = {"PR": pr_no, "RFQ": rfq_no, "RES": res_no, "PO": po_no}
    report["generated_at"] = now()
    return docs, report


# ---------------------------------------------------------- chain checks

def validate_chain(docs):
    """Cross-document consistency checks. Fail closed: any violation blocks
    PDF rendering. These encode exactly the manual-error classes observed in
    real sample documents (words/figures mismatch, fund-cluster drift)."""
    v, notes = [], []

    def data(key):
        return docs[key]["data"]

    pr, rfq = data("purchase_request"), data("request_for_quotation")
    minutes, abstract = data("minutes_of_opening"), data("abstract_of_bids")
    resolution, po = data("bac_resolution_award"), data("purchase_order")

    # totals are computed, not trusted
    pr_total, pr_v = compute_items([dict(i) for i in pr["items"]])
    v += [f"purchase_request: {x}" for x in pr_v]
    if round(pr_total, 2) != round(pr["total_amount"], 2):
        v.append(f"purchase_request: line totals ({pr_total}) != total_amount ({pr['total_amount']})")
    po_total, po_v = compute_items([dict(i) for i in po["items"]])
    v += [f"purchase_order: {x}" for x in po_v]
    if round(po_total, 2) != round(po["total_amount"], 2):
        v.append(f"purchase_order: line totals ({po_total}) != total_amount ({po['total_amount']})")

    # words vs figures (the manual-error class this module exists to kill)
    expected_words = amount_in_words(po["total_amount"])
    if str(po.get("amount_in_words", "")).strip().upper() != expected_words:
        v.append("purchase_order: amount_in_words does not match total_amount "
                 f"(expected '{expected_words}')")

    # fund cluster consistency across the chain
    if str(pr.get("fund_cluster", "")).strip() != str(po.get("fund_cluster", "")).strip():
        v.append("fund_cluster differs between purchase_request and purchase_order "
                 f"('{pr.get('fund_cluster')}' vs '{po.get('fund_cluster')}')")

    # ABC consistency and award ceiling
    if round(rfq.get("abc", 0), 2) != round(pr["total_amount"], 2):
        v.append("request_for_quotation: abc != purchase_request total_amount")
    if round(po["total_amount"], 2) > round(rfq.get("abc", 0), 2):
        v.append("award exceeds ABC (purchase_order total > request_for_quotation abc)")

    # cross references
    if rfq.get("pr_number_ref") != pr.get("pr_number"):
        v.append("request_for_quotation.pr_number_ref != purchase_request.pr_number")
    if minutes.get("rfq_number_ref") != rfq.get("rfq_number"):
        v.append("minutes_of_opening.rfq_number_ref != request_for_quotation.rfq_number")
    if abstract.get("rfq_number_ref") != rfq.get("rfq_number"):
        v.append("abstract_of_bids.rfq_number_ref != request_for_quotation.rfq_number")
    if resolution.get("pr_number_ref") != pr.get("pr_number"):
        v.append("bac_resolution_award.pr_number_ref != purchase_request.pr_number")
    if round(po["total_amount"], 2) != round(resolution.get("awarded_amount", -1), 2):
        v.append("purchase_order total != bac_resolution_award awarded_amount")

    # LCRB logic: the awarded supplier must be the lowest total bid
    bids = [s for s in (minutes.get("supplier_bids") or []) if isinstance(s, dict)]
    amounts = [b.get("bid_amount") for b in bids if isinstance(b.get("bid_amount"), (int, float))]
    if not bids:
        v.append("minutes_of_opening: no supplier bids recorded")
    elif amounts:
        lowest = min(amounts)
        lowest_names = sorted(b.get("supplier", "") for b in bids
                              if b.get("bid_amount") == lowest)
        if resolution.get("awarded_supplier") not in lowest_names:
            v.append("awarded_supplier is not the lowest calculated bid "
                     f"(lowest: {', '.join(lowest_names)})")
        if abstract.get("lcrb_supplier") and abstract["lcrb_supplier"] not in lowest_names:
            v.append("abstract_of_bids.lcrb_supplier is not the lowest calculated bid")
    if len(bids) < 3:
        notes.append(f"only {len(bids)} quotation(s) recorded; IRR Sec. 34 requires "
                     ">=3 suppliers invited (a single quotation may suffice, "
                     "BAC Secretariat confirms)")

    # date chain
    def d(s):
        try:
            return date.fromisoformat(str(s)[:10])
        except (TypeError, ValueError):
            return None

    dl, op = d(rfq.get("submission_deadline_date")), d(minutes.get("opening_date"))
    if dl and op and op < dl:
        v.append("opening date is before the RFQ submission deadline")
    res = d(resolution.get("resolved_date"))
    if op and res and res < op:
        v.append("resolution date is before the opening date")
    dv, pd_ = d(po.get("delivery_date")), d(po.get("po_date"))
    if dv and pd_ and dv < pd_:
        v.append("delivery date is before the PO date")

    return {"ok": not v, "violations": v, "notes": notes}


# ------------------------------------------------------------- rendering

def _draw_common(page, spec):
    y = 54
    for line in spec.get("header", []):
        page.text(pw.PAGE_WIDTH / 2, y, 10.5, line, center=pw.PAGE_WIDTH / 2)
        y += 15
    y += 6
    page.rect(54, y, pw.PAGE_WIDTH - 108, 22, width=1.2)
    page.text(pw.PAGE_WIDTH / 2, y + 15, 12.5, spec["title"], bold=True,
              center=pw.PAGE_WIDTH / 2)
    y += 22
    if spec.get("title_line2"):
        y += 14
        page.text(pw.PAGE_WIDTH / 2, y, 9.5, spec["title_line2"], bold=True,
                  center=pw.PAGE_WIDTH / 2)
    return y + 14


def _draw_meta_rows(page, rows, y):
    for row in rows:
        l1, v1, l2, v2 = (list(row) + ["", "", "", ""])[:4]
        if l2 == "" and v2 == "":
            page.text(54, y, 8.5, l1, bold=True)
            page.text(200, y, 8.5, v1)
            y += 14
        else:
            page.text(54, y, 8.5, l1, bold=True)
            page.text(200, y, 8.5, str(v1))
            page.text(320, y, 8.5, l2, bold=True)
            page.text(470, y, 8.5, str(v2))
            y += 14
    return y + 6


def _draw_table(page, table, y):
    headers = table["headers"]
    xs = [54 + i * ((pw.PAGE_WIDTH - 128) / max(len(headers), 1)) for i in range(len(headers))]
    right = set(table.get("col_right", []))
    row_h = 16
    page.rect(54, y, pw.PAGE_WIDTH - 108, row_h)
    for x, h in zip(xs, headers):
        page.text(x + 3, y + 11, 8, h, bold=True)
    for i in range(1, len(headers)):
        page.line(xs[i], y, xs[i], y + row_h)
    y += row_h
    for row in table.get("rows", []):
        page.rect(54, y, pw.PAGE_WIDTH - 108, row_h)
        for i, (x, cell) in enumerate(zip(xs, row)):
            text = str(cell)
            lines = text.split("\n") or [""]
            for j, ln in enumerate(lines):
                if i in right:
                    page.text(x + ((pw.PAGE_WIDTH - 108) / len(headers)) - 4,
                              y + 11 + j * 10, 7.5, ln, right=xs[i] + (pw.PAGE_WIDTH - 108) / len(headers) - 4)
                else:
                    page.text(x + 3, y + 11 + j * 10, 7.5, ln)
        for i in range(1, len(headers)):
            page.line(xs[i], y, xs[i], y + row_h)
        y += row_h
    return y + 8


def _draw_signatories(page, signatories, y):
    slots = max(len(signatories), 1)
    slot_w = (pw.PAGE_WIDTH - 108) / slots
    for i, (name, role) in enumerate(signatories[:5]):
        x = 54 + i * slot_w + slot_w / 2
        page.line(x - 80, y + 22, x + 80, y + 22)
        page.text(x, y + 34, 8.5, name or "________________", center=x)
        page.text(x, y + 46, 7.5, role or "", center=x)
    return y + 60


def render_doc_pdf(spec):
    """Render one document spec into a PDF (bytes)."""
    pdf = pw.PDF()
    page = pdf.add_page()
    y = _draw_common(page, spec)
    if spec.get("meta_rows"):
        y = _draw_meta_rows(page, spec["meta_rows"], y)
    if spec.get("notice"):
        page.text(54, y, 8.5, spec["notice"])
        y += 14
        for line in spec.get("notice_person", []):
            page.text(72, y, 8.5, line)
            y += 12
        y += 6
    if spec.get("attendance"):
        page.text(54, y, 8.5, "PRESENT:", bold=True)
        y += 14
        for name, role in spec["attendance"]:
            page.text(72, y, 8.5, name, bold=True)
            page.text(260, y, 8.5, role)
            y += 13
        y += 6
    if spec.get("supplier_totals"):
        for name, amount in spec["supplier_totals"]:
            page.text(72, y, 8.5, name, bold=True)
            page.text(280, y, 8.5, "Bid amount: " + amount)
            y += 13
        y += 6
    if spec.get("narrative"):
        page.text(54, y, 8.5, spec["narrative"])
        y += 24
    if spec.get("whereas"):
        for para in spec["whereas"]:
            page.text(54, y, 8.5, para)
            y += 38
        y += 4
    if spec.get("resolved_line"):
        page.text(54, y, 8.5, spec["resolved_line"])
        y += 18
    if spec.get("table"):
        y = _draw_table(page, spec["table"], y)
    if spec.get("purpose"):
        page.rect(54, y, pw.PAGE_WIDTH - 108, 26)
        page.text(60, y + 11, 8.5, "Purpose:", bold=True)
        page.text(120, y + 11, 8.5, spec["purpose"])
        y += 34
    if spec.get("words_row"):
        left, rightv = spec["words_row"]
        page.rect(54, y, pw.PAGE_WIDTH - 108, 18)
        page.text(60, y + 12, 8, left, bold=True)
        page.text(pw.PAGE_WIDTH - 60, y + 12, 9, rightv, bold=True,
                  right=pw.PAGE_WIDTH - 60)
        y += 26
    if spec.get("extra_blocks"):
        for label, value in spec["extra_blocks"]:
            page.text(54, y, 8.5, label, bold=True)
            page.text(170, y, 8.5, str(value))
            y += 13
        y += 6
    if spec.get("signatories"):
        y = max(y, 600)
        _draw_signatories(page, spec["signatories"], y)
    # mandatory draft banner on every document
    page.rect(54, pw.PAGE_HEIGHT - 46, pw.PAGE_WIDTH - 108, 18, width=1.0)
    page.text(pw.PAGE_WIDTH / 2, pw.PAGE_HEIGHT - 33, 8, DRAFT_BANNER,
              center=pw.PAGE_WIDTH / 2)
    return pdf


def render_pdfs(docs, out_dir, *, allow_invalid=False):
    """Write one PDF per document + manifest.json. Refuses to render when
    validate_chain found violations unless allow_invalid=True (and then every
    page is stamped with the violations banner)."""
    violations = docs.get("_violations") or []
    report = docs.get("_report") or {}
    if not report:
        report = validate_chain(docs)
    if report["violations"] and not allow_invalid:
        raise ValueError(
            "chain failed validation; refusing to render PDFs: "
            + "; ".join(report["violations"]))
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for key in DOC_ORDER:
        doc = docs[key]
        spec = doc["render"]
        if report["violations"]:
            spec = dict(spec)
            spec["header"] = list(spec.get("header", [])) + [
                "!! DRAFT CONTAINS VALIDATION VIOLATIONS !!"]
        pdf = render_doc_pdf(spec)
        path = os.path.join(out_dir, doc.get("file_hint", key + ".pdf"))
        pdf.write_file(path)
        written.append(path)
    manifest = {
        "generated_at": now(),
        "advisory": True,
        "status": "DRAFT - AI-ASSISTED (unsigned)",
        "violations": report["violations"],
        "notes": report.get("notes", []),
        "numbering": report.get("numbering"),
        "files": written,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return written, manifest


# ------------------------------------------------------------------- CLI

def _demo_params():
    """Fully fictional parameters shaped like a real SVP cycle (no real
    persons/suppliers, per the data-governance rule)."""
    return {
        "entity_name": "Department of Information and Communications Technology",
        "header_address": "Regional Office 02 - Sample Address, Sample City",
        "end_user": "DICT Regional Office 02",
        "office_section": "END-USER UNIT",
        "responsibility_center_code": "0100000000000000",
        "fund_cluster": "Regular Fund",
        "purpose": "Procurement of Sample Training Kits for a Sample Training Program (Province A & Province B).",
        "mode_of_procurement": "Small Value Procurement",
        "pr_date": str(date.today()),
        "deadline_date": str(date.today() + timedelta(days=10)),
        "deadline_time": "9:00 AM",
        "opening_date": str(date.today() + timedelta(days=10)),
        "opening_time": "11:00 AM",
        "delivery_date": str(date.today() + timedelta(days=16)),
        "place_of_delivery": "DICT Regional Office 02",
        "payment_term": "within 30 working days upon Inspection and Acceptance",
        "accountant": "SAMPLE ACCOUNTANT, Accountant III",
        "requested_by": {"name": "JUAN A. DEMO", "designation": "Chief, END-USER UNIT"},
        "approved_by": {"name": "MARIA B. SAMPLE", "designation": "Regional Director"},
        "bac_chairperson": {"name": "PEDRO C. SAMPLE"},
        "bac_members": [
            {"name": "PEDRO C. SAMPLE", "role": "BAC Chairperson"},
            {"name": "ANA D. DEMO", "role": "BAC Vice Chairperson"},
            {"name": "LUIS E. TEST", "role": "Member"},
            {"name": "CARMEN F. SAMPLE", "role": "Member"},
        ],
        "secretariat_head": {"name": "PEPE H. TEST"},
        "contact_email": "bac.sample@example.gov.ph",
        "required_documents": [
            "Latest Business/Mayor's Permit",
            "PhilGEPS Certificate of Registration or Registration Number",
        ],
        "items": [
            {"stock_no": "1", "unit": "pc", "description": "Training Kits\nTraining Polo Shirt",
             "quantity": 50, "unit_cost": 514.0},
            {"stock_no": "2", "unit": "pc", "description": "Other Supplies\nPull-Up Banner 3 x 4 ft.",
             "quantity": 2, "unit_cost": 1000.0},
        ],
        "suppliers": [
            {"supplier": "SAMPLE SUPPLIER A", "bid_amount": 26100.0},
            {"supplier": "SAMPLE SUPPLIER B", "bid_amount": 27400.0},
            {"supplier": "SAMPLE SUPPLIER C", "bid_amount": 28000.0},
        ],
        "award": {
            "supplier": "SAMPLE SUPPLIER A", "bid_amount": 26100.0,
            "address": "Sample Street, Sample City", "tin": "000-000-000-000",
            "representative": "JUAN SAMPLE-REP",
            "items": [
                {"stock_no": "1", "unit": "pc", "description": "Training Kits\nTraining Polo Shirt",
                 "quantity": 50, "unit_cost": 450.0},
                {"stock_no": "2", "unit": "pc", "description": "Other Supplies\nPull-Up Banner 3 x 4 ft.",
                 "quantity": 2, "unit_cost": 1800.0},
            ],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Draft the SVP document chain (AI-assisted, fail-closed)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("demo", help="Draft the full chain for a fictional scenario")
    p.add_argument("--db", default=":memory:")
    p.add_argument("--out", default="./svp-output")
    p.add_argument("--allow-invalid", action="store_true",
                   help="render even when validation fails (pages are stamped)")

    p = sub.add_parser("words", help="Show amount-in-words for a value")
    p.add_argument("--amount", required=True, type=float)

    args = parser.parse_args(argv)
    if args.cmd == "words":
        print(amount_in_words(args.amount))
        return 0
    if args.cmd == "demo":
        from core import init_db
        conn = init_db(args.db, seed=False)
        try:
            docs, report = draft_chain(conn, _demo_params())
        finally:
            conn.close()
        written, manifest = render_pdfs(docs, args.out,
                                        allow_invalid=args.allow_invalid)
        print(json.dumps({
            "out": args.out,
            "files": written,
            "ok": report["ok"],
            "violations": report["violations"],
            "notes": report.get("notes", []),
            "numbering": report.get("numbering"),
        }, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
