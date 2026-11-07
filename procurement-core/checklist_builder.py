"""Deterministic checklist builder per mode/regime (Track B / mode coverage).

Builds a BAC-approvable required-document checklist from the verified rules
registry — never from model output. The checklist still needs a named Human
("approved_by") before checker.evaluate_submission accepts it, and the BAC
Secretariat remains the authority for what a real transaction requires.

Coverage (RA12009_IRR only — legacy RA9184_2016IRR is unverified and fails
closed via get_rule/UnverifiedRuleError, by design):

- Competitive Bidding: PhilGEPS + Omnibus + Bid Securing Declaration +
  Financial Proposal (ABC-aware amount check).
- Small Value Procurement: PhilGEPS + Omnibus + Bid Securing Declaration +
  Financial Proposal where the approver enables it (SVP sometimes proceeds on
  a single quotation — the checklist is per-transaction, not auto-imposed).
- Any other mode: fails closed with a reason; do NOT invent required
  documents. A BAC officer must define the checklist for the mode instead.

The required-document sets above are a starting template for BAC review, not
a legal opinion. Section references point at IRR RA 12009 Sec. 59.1 (checklist
of required documents) as verified in seed_corpus.json.
"""

from __future__ import annotations

import argparse
import json
import os

from core import CoreError, _connect, canonical, get_tx, init_db, resolve_rules, get_rule

# Verification anchor: corpus chunks verified in seed_corpus.json.
# If the corpus snapshot lacks Sec. 59.1, callers must not treat the
# section_ref as a verified citation — the RAG resolver will ABSTAIN.
SECTION_REF = "IRR RA 12009 Sec. 59.1 / ITB checklist"

# Template per mode: list of (key, checks). Checks reference checker.py types.
_COMPETITIVE = [
    ("philgeps_registration", [{"type": "present"}, {"type": "not_expired"}]),
    ("omnibus_sworn_statement", [{"type": "present"}, {"type": "signed"}, {"type": "notarized"}]),
    ("bid_securing_declaration", [{"type": "present"}, {"type": "signed"}]),
    ("financial_proposal", [{"type": "present"}, {"type": "amount_lte_abc"}]),
]

_SVP_BASE = [
    ("philgeps_registration", [{"type": "present"}, {"type": "not_expired"}]),
    ("omnibus_sworn_statement", [{"type": "present"}, {"type": "signed"}, {"type": "notarized"}]),
    ("bid_securing_declaration", [{"type": "present"}, {"type": "signed"}]),
]

_SVP_WITH_FINANCIAL = _SVP_BASE + [
    ("financial_proposal", [{"type": "present"}, {"type": "amount_lte_abc"}]),
]

_TEMPLATES = {
    "Competitive Bidding": _COMPETITIVE,
    "Small Value Procurement": _SVP_BASE,
}

VALID_MODES = sorted(_TEMPLATES)


def supported_modes():
    return list(VALID_MODES)


def build_checklist(conn, tx_id, *, approved_by, abc=None,
                    include_financial_for_svp=False, note=""):
    """Build a checklist for a transaction. Fails closed when unverified.

    - approved_by must be a named human (BAC Secretariat officer).
    - abc (Approved Budget for the Contract) is required when the mode
      template includes an amount_lte_abc check; otherwise None is fine.
    - For SVP, include_financial_for_svp=True appends the financial proposal
      check; default False because SVP may proceed on fewer documents per
      the per-transaction checklist authority.
    """
    if not approved_by:
        raise CoreError("approved_by (a named BAC Secretariat officer) is required")
    tx = get_tx(conn, tx_id)
    mode = tx["mode"]
    if mode not in _TEMPLATES:
        raise CoreError(
            f"no verified checklist template for mode '{mode}' "
            f"(supported: {VALID_MODES}); BAC Secretariat must define the "
            "checklist for this mode — never auto-invent required documents"
        )
    resolved = resolve_rules(conn, tx["posting_date"], tx["regime"])
    # Fail closed: raises UnverifiedRuleError for legacy/unverified regimes.
    get_rule(conn, resolved, "CHECKLIST_AUTHORITY")
    if resolved["ruleset_version"] != tx["ruleset_version"]:
        raise CoreError("ruleset_version does not match the pinned transaction ruleset")

    template = list(_TEMPLATES[mode])
    if mode == "Small Value Procurement" and include_financial_for_svp:
        template = list(_SVP_WITH_FINANCIAL)

    documents = []
    for key, checks in template:
        filled_checks = []
        for check in checks:
            check = dict(check)
            if check["type"] == "amount_lte_abc":
                if abc is None:
                    raise CoreError(
                        "abc is required for amount_lte_abc checks "
                        "(Approved Budget for the Contract from approved sources)"
                    )
                check["limit"] = abc
            filled_checks.append(check)
        documents.append({"key": key, "section_ref": SECTION_REF, "checks": filled_checks})

    checklist = {
        "approved_by": approved_by,
        "ruleset_version": tx["ruleset_version"],
        "regime": tx["regime"],
        "mode": mode,
        "note": note or f"Generated template for {mode}; BAC Secretariat approval required.",
        "documents": documents,
        "advisory": True,
        "template_only": True,
    }
    return checklist


def validate_checklist_for_checker(checklist, conn, tx_id):
    """Mirror checker.evaluate_submission preconditions without evaluating.

    Returns warnings (empty = ready). Raises CoreError for hard failures
    that checker would also reject.
    """
    if not checklist.get("approved_by"):
        raise CoreError("checklist must carry approved_by")
    tx = get_tx(conn, tx_id)
    if checklist.get("ruleset_version") != tx["ruleset_version"]:
        raise CoreError("checklist ruleset_version does not match tx ruleset")
    resolved = resolve_rules(conn, tx["posting_date"], tx["regime"])
    get_rule(conn, resolved, "CHECKLIST_AUTHORITY")
    warnings = []
    if checklist.get("template_only"):
        warnings.append("template-generated checklist; BAC review/approval recorded in approved_by")
    return warnings


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a mode/regime checklist template (requires BAC approval before use)."
    )
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "procurement.db"))
    parser.add_argument("--tx-id", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--abc", type=float, default=None)
    parser.add_argument("--svp-financial", action="store_true",
                        help="For SVP: include financial proposal check")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    conn = init_db(args.db, seed=not os.path.exists(args.db)) if not os.path.exists(args.db) else _connect(args.db)
    try:
        checklist = build_checklist(
            conn, args.tx_id, approved_by=args.approved_by, abc=args.abc,
            include_financial_for_svp=args.svp_financial,
        )
        text = json.dumps(checklist, indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(canonical({"wrote": args.out, "mode": checklist["mode"], "documents": len(checklist["documents"])}))
        else:
            print(text)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
