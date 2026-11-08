"""Track B / go-live readiness report for ProcEMP seed data and registries.

Audits rules, corpus, and forms for verification status, placeholders, and
release blockers. This is decision support for BAC/legal/DPO review, not a
legal opinion. It reports fail-closed blockers explicitly.
"""

from __future__ import annotations

import argparse
import json
import os

from core import SEED_CORPUS_PATH, SEED_FORMS_PATH, SEED_PATH, _connect, canonical, now

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def audit_rules(path=SEED_PATH):
    data = _load(path)
    rules = data.get("rules", [])
    unverified = []
    placeholders = []
    for r in rules:
        if not r.get("verified"):
            unverified.append({
                "ruleset_id": r.get("ruleset_id"), "key": r.get("key"),
                "section_ref": r.get("section_ref"), "note": r.get("note"),
            })
        value = r.get("value")
        if value == {"status": "UNVERIFIED"} or (isinstance(value, dict) and value.get("status") == "UNVERIFIED"):
            placeholders.append({"ruleset_id": r.get("ruleset_id"), "key": r.get("key")})
    transition = data.get("transition_policy", {})
    return {
        "source": os.path.basename(path),
        "transition_policy_confirmed": transition.get("status") == "CONFIRMED",
        "transition_policy": transition,
        "rules_total": len(rules),
        "rules_verified": len(rules) - len(unverified),
        "rules_unverified": unverified,
        "placeholder_values": placeholders,
        "fail_closed_impact": "Unverified rules cause UnverifiedRuleError unless explicitly overridden; placeholders must be confirmed before use.",
    }


def audit_corpus(path=SEED_CORPUS_PATH):
    data = _load(path)
    chunks = data.get("chunks", [])
    unverified = [c for c in chunks if not c.get("verified")]
    return {
        "source": os.path.basename(path),
        "corpus_snapshot_id": data.get("corpus_snapshot_id"),
        "chunks_total": len(chunks),
        "chunks_verified": len(chunks) - len(unverified),
        "unverified_chunks": [{
            "chunk_id": c.get("chunk_id"), "section": c.get("section"),
            "regime": c.get("regime"), "reason": "verified=false; cannot be cited",
        } for c in unverified],
        "fail_closed_impact": "verified=false chunks can never produce verified citations; citation checks force ABSTAIN.",
    }


def audit_forms(path=SEED_FORMS_PATH):
    data = _load(path)
    forms = data.get("forms", [])
    missing_body = []
    by_class = {}
    for f in forms:
        cls = f.get("classification", "UNKNOWN")
        by_class[cls] = by_class.get(cls, 0) + 1
        # seed may omit these fields; load_forms defaults to unavailable/unverified.
        body_available = bool(f.get("body_available")) and bool(f.get("official_file_hash"))
        if not body_available:
            missing_body.append({
                "code": f.get("code"), "name": f.get("name"),
                "classification": cls, "category": f.get("category"),
            })
    return {
        "source": os.path.basename(path),
        "forms_total": len(forms),
        "by_classification": by_class,
        "official_bodies_registered": len(forms) - len(missing_body),
        "missing_official_bodies": missing_body,
        "online_platform_caveat": data.get("online_platform_caveat"),
        "fail_closed_impact": "Without registered official form bodies/hashes, release-ready legal form output is blocked; drafts remain structural/labelled.",
    }


def audit_db(db_path):
    conn = _connect(db_path)
    try:
        def count(sql, params=()):
            return conn.execute(sql, params).fetchone()["c"]
        forms_missing = count(
            "SELECT count(*) c FROM form_registry WHERE body_available=0 OR official_file_hash IS NULL OR verified=0"
        )
        snapshots = conn.execute(
            "SELECT id, regime, approved_by, is_active, checksum FROM corpus_snapshot"
        ).fetchall()
        return {
            "db": db_path,
            "form_registry_total": count("SELECT count(*) c FROM form_registry"),
            "form_registry_not_release_ready": forms_missing,
            "corpus_snapshots": [dict(r) for r in snapshots],
            "review_items_open": count("SELECT count(*) c FROM review_item WHERE status='OPEN'"),
            "verdicts_total": count("SELECT count(*) c FROM verdict"),
        }
    finally:
        conn.close()


def build_report(db_path=None):
    report = {
        "generated_at": now(),
        "advisory_only": True,
        "not_a_legal_opinion": True,
        "rules": audit_rules(),
        "corpus": audit_corpus(),
        "forms": audit_forms(),
    }
    if db_path and os.path.exists(db_path):
        report["database"] = audit_db(db_path)
    report["blockers"] = blockers(report)
    report["ready_for_production"] = len(report["blockers"]) == 0
    return report


def blockers(report):
    b = []
    rules = report["rules"]
    corpus = report["corpus"]
    forms = report["forms"]
    if not rules["transition_policy_confirmed"]:
        b.append("Transition policy unconfirmed (GPPB guidance / legal officer decision required per transaction context).")
    if rules["rules_unverified"]:
        b.append(f"{len(rules['rules_unverified'])} unverified rule(s) present; fail closed unless explicitly allowed.")
    if rules["placeholder_values"]:
        b.append(f"{len(rules['placeholder_values'])} placeholder UNVERIFIED rule value(s) must be confirmed.")
    if corpus["unverified_chunks"]:
        b.append(f"{len(corpus['unverified_chunks'])} unverified corpus chunk(s) cannot be cited.")
    if forms["missing_official_bodies"]:
        b.append(f"{len(forms['missing_official_bodies'])} form(s) lack registered official bodies/hashes; release-ready output blocked.")
    b.append("External confirmations required: DPIA/RA 10173/NPC, e-sign/wet-ink basis, PhilGEPS approach, GPPB online-form authorization, official form bodies, full verified corpus, real model+benchmark+human-gated training.")
    return b


def render_worklist(report):
    """Render the Track B worklist (07-readiness-worklist.md) from live data.

    Regenerate with: python readiness_report.py --format worklist --out ... 
    Every item is [HUMAN/EXTERNAL] unless it is an engineering follow-through;
    values are never invented — the generator only restates audited state.
    """
    rules = report["rules"]
    corpus = report["corpus"]
    forms = report["forms"]
    lines = [
        "# Readiness Worklist (Track B) — derived from `readiness_report.py`",
        "",
        "> **Advisory / not a legal opinion.** This worklist tracks fail-closed blockers and required confirmations.",
        "> Items marked **[HUMAN/EXTERNAL]** require BAC Secretariat, Legal, DPO, IT/Security, or GPPB/COA action — do **not** guess values.",
        f"> Regenerate: `cd procurement-core && python readiness_report.py --format worklist --out ../procurement-review/07-readiness-worklist.md`.",
        "",
        f"**Generated from report:** {report['generated_at']} (prototype). `ready_for_production` (data/config checks only): **{str(report['ready_for_production']).lower()}**.",
        "",
        "## 1. Transition policy / regime selection [HUMAN/EXTERNAL]",
        "- [ ] **Owner: Legal/BAC Secretariat.** Obtain/record GPPB guidance on RA 9184 vs RA 12009 applicability during the transitory period (IRR RA 12009 Sec. 113; GPPB Res. 03-2025 context noted in `seed_rules.json`).",
        "- [ ] Until confirmed: `suggest_regime()` remains `AMBIGUOUS` where applicable; **legal officer must choose regime explicitly per transaction** with recorded `legal_basis_note`. Do not auto-select.",
        "- [ ] When confirmed: update transition policy/rules data with named approver, authority citation, effective dates; keep versioned as-of behavior.",
        "",
        "## 2. Unverified / placeholder rules (`seed_rules.json`) [HUMAN/EXTERNAL]",
        f"Fail-closed impact: {rules['fail_closed_impact']} ({rules['rules_verified']} verified / {len(rules['rules_unverified'])} unverified).",
        "",
        "| Ruleset | Key | Section ref | Action (confirm with authority; then update verified/value) | Status |",
        "|---|---|---|---|---|",
    ]
    if rules["rules_unverified"]:
        for r in rules["rules_unverified"]:
            lines.append(
                f"| `{r.get('ruleset_id')}` | `{r.get('key')}` | {r.get('section_ref')} | "
                f"{r.get('note') or r.get('section_ref')} | [ ] |"
            )
    else:
        lines.append("| — | — | — | all rules verified | [x] |")
    lines += [
        "",
        "## 3. Corpus (`seed_corpus.json`) [HUMAN/EXTERNAL]",
        f"Snapshot: `{corpus['corpus_snapshot_id']}` — {corpus['chunks_total']} chunks, "
        f"{corpus['chunks_verified']} verified, {len(corpus['unverified_chunks'])} unverified. "
        f"`verified=false` cannot produce verified citations (fail closed → ABSTAIN).",
        "",
    ]
    if corpus["unverified_chunks"]:
        for c in corpus["unverified_chunks"]:
            lines.append(
                f"- [ ] **Owner: Legal/BAC Secretariat.** Replace `{c.get('chunk_id')}` "
                f"({c.get('regime')}, {c.get('section')}) with verified, verbatim/approved source text + metadata, "
                "or keep excluded and document why legacy citations are unavailable."
            )
    else:
        lines.append("- [x] All corpus chunks verified; continue expanding the verified set.")
    lines += [
        "- [ ] Expand to full verified GPPB/COA/issuance set needed for production scope; "
        "create a new `corpus_snapshot_id` for approved changes (never silently repin transactions).",
        "- [ ] Run corpus integrity/citation verification tests after updates.",
        "",
        "## 4. Official form bodies / hashes (`seed_forms.json`) [HUMAN/EXTERNAL]",
        f"{forms['forms_total']} forms total "
        f"({(forms.get('by_classification') or {}).get('STANDARD', 0)} STANDARD, "
        f"{(forms.get('by_classification') or {}).get('RECOMMENDED', 0)} RECOMMENDED), "
        f"**{forms['official_bodies_registered']} official bodies registered**. "
        "Release-ready legal form output is blocked; drafts remain structural/labelled.",
        "",
        "- [ ] **Owner: Authorized form custodian (named human).** Obtain official GPPB/agency-approved "
        "form bodies/files; compute/register hashes per `forms.py`/registry process; record who/when/authority.",
        f"- [ ] **{len(forms['missing_official_bodies'])} codes** currently missing official bodies "
        "(see readiness report JSON for full list): "
        + (", ".join(f["code"] for f in forms["missing_official_bodies"]) if forms["missing_official_bodies"] else "none"),
        "- [ ] **GPPB online-form authorization:** confirm agency authorization before treating any "
        "system-generated/accomplished form as official (Res. 03-2025 caveat recorded in report).",
        "",
        "## 5. External confirmations (production gate) [HUMAN/EXTERNAL]",
        "- [ ] DPIA/RA 10173/NPC obligations and approvals (see `05-data-governance-training.md`).",
        "- [ ] E-signature basis (PNPKI/RA 8792) vs wet-ink procedure.",
        "- [ ] PhilGEPS posting/API approach.",
        "- [ ] GPPB authorization for online accomplishment of Standard Forms (as applicable).",
        "- [ ] Full verified legal/corpus scope for deployment.",
        "- [ ] Real model + benchmark + human-gated training path (no auto-deploy).",
        "",
        "## 6. Engineering follow-through (implemented in codebase)",
        "- [ ] Re-run `readiness_report.py` after each verified data change; keep diffs audited.",
        "- [ ] Add/maintain tests for any rule/corpus/form parsing or registry behavior changes.",
        "- [ ] Review server (Track C) hardening: authenticated reverse proxy/TLS for non-loopback, "
        "richer evidence viewers, runbooks — keep advisory/audited, preserve human gates.",
        "- [ ] Extraction (Track A): only with approved/anonymized samples + local loopback runtime; "
        "plumbing check before fine-tune; frozen-slice non-regression.",
        "",
        "## Definition of “data/config ready” (not legal sign-off)",
        "`readiness_report` reports no fail-closed data blockers for the *checked* seeds/registry "
        "and external confirmations are documented — **still requires legal/DPO/BAC/COA/governance "
        "approvals before production use**.",
    ]
    return "\n".join(lines) + "\n"


def render_markdown(report):
    lines = [
        "# ProcEMP Readiness Report",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Advisory only / not a legal opinion: {report['advisory_only']}",
        f"- Ready for production (data/config checks only): **{report['ready_for_production']}**",
        "",
        "## Blockers / fail-closed items",
    ]
    for b in report["blockers"]:
        lines.append(f"- {b}")
    for section in ("rules", "corpus", "forms"):
        s = report[section]
        lines += ["", f"## {section.title()} ({s['source']})", "```json", json.dumps(s, indent=2, ensure_ascii=False), "```"]
    if "database" in report:
        lines += ["", "## Database", "```json", json.dumps(report["database"], indent=2, ensure_ascii=False), "```"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="ProcEMP readiness/audit report (advisory)")
    parser.add_argument("--db", help="Optional SQLite DB to include registry stats")
    parser.add_argument("--format", choices=["json", "md", "worklist"], default="json")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    report = build_report(args.db)
    if args.format == "md":
        text = render_markdown(report)
    elif args.format == "worklist":
        text = render_worklist(report)
    else:
        text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(canonical({"wrote": args.out, "blockers": len(report["blockers"]),
                         "ready_for_production": report["ready_for_production"]}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())