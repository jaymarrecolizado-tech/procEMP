# Readiness Worklist (Track B) — derived from `readiness_report.py`

> **Advisory / not a legal opinion.** This worklist tracks fail-closed blockers and required confirmations.
> Items marked **[HUMAN/EXTERNAL]** require BAC Secretariat, Legal, DPO, IT/Security, or GPPB/COA action — do **not** guess values.
> Regenerate: `cd procurement-core && python readiness_report.py --format md` (or `--out ../procurement-review/readiness-latest.md`).

**Generated from report:** 2026-11-08 (prototype). `ready_for_production` (data/config checks only): **false**.

## 1. Transition policy / regime selection [HUMAN/EXTERNAL]
- [ ] **Owner: Legal/BAC Secretariat.** Obtain/record GPPB guidance on RA 9184 vs RA 12009 applicability during the transitory period (IRR RA 12009 Sec. 113; GPPB Res. 03-2025 context noted in `seed_rules.json`).
- [ ] Until confirmed: `suggest_regime()` remains `AMBIGUOUS` where applicable; **legal officer must choose regime explicitly per transaction** with recorded `legal_basis_note`. Do not auto-select.
- [ ] When confirmed: update transition policy/rules data with named approver, authority citation, effective dates; keep versioned as-of behavior.

## 2. Unverified / placeholder rules (`seed_rules.json`) [HUMAN/EXTERNAL]
Fail-closed impact: unverified rules cause `UnverifiedRuleError` unless explicitly overridden in controlled flows; placeholders must be confirmed before use.

| Ruleset | Key | Section ref | Action (confirm with authority; then update verified/value) | Status |
|---|---|---|---|---|
| `rs-ra9184-2016` | `CHECKLIST_AUTHORITY` | 2016 IRR Sec. 30 / eligibility checklist | Confirm legacy eligibility checklist authority; register approved checklist | [ ] |
| `rs-ra9184-2016` | `MODES` | 2016 Revised IRR RA 9184 Sec. 48-53 | Confirm against consolidated 2016 IRR | [ ] |
| `rs-ra9184-2016` | `SVP_THRESHOLD` | 2016 IRR Annex H Sec. 53.9 + GPPB threshold amendments | BAC Secretariat confirms applicable GPPB amendment in force on transaction date; populate exact value | [ ] |
| `rs-ra9184-2016` | `SHOPPING_THRESHOLD` | 2016 IRR Annex H Sec. 52.1 | Confirm and populate | [ ] |
| `rs-ra9184-2016` | `POSTING_DAYS_COMPETITIVE` | 2016 IRR Sec. 21.2 | Confirm (varies by ABC); populate | [ ] |

## 3. Corpus (`seed_corpus.json`) [HUMAN/EXTERNAL]
Snapshot: `corpus-ra12009-2025-02` — 14 chunks, 13 verified, 1 unverified. `verified=false` cannot produce verified citations (fail closed → ABSTAIN).

- [ ] **Owner: Legal/BAC Secretariat.** Replace `LEGACY-ANNEX-H-PLACEHOLDER` (RA9184_2016IRR, Annex H) with verified, verbatim/approved source text + metadata, or keep excluded and document why legacy citations are unavailable.
- [ ] Expand to full verified GPPB/COA/issuance set needed for production scope; create a new `corpus_snapshot_id` for approved changes (never silently repin transactions).
- [ ] Run corpus integrity/citation verification tests after updates.

## 4. Official form bodies / hashes (`seed_forms.json`) [HUMAN/EXTERNAL]
36 forms total (14 STANDARD, 22 RECOMMENDED), **0 official bodies registered**. Release-ready legal form output is blocked; drafts remain structural/labelled.

- [ ] **Owner: Authorized form custodian (named human).** Obtain official GPPB/agency-approved form bodies/files; compute/register hashes per `forms.py`/registry process; record who/when/authority.
- [ ] **All 36 codes** currently missing official bodies (see readiness report JSON for full list): APP, PPMP, MMSF, PBD_GOODS, PBD_INFRA, PBD_CONSULTING, CONTRACT_FORM, OSS, BSD, PSD, BID_FORM, PRICE_SCHEDULE, TECHNICAL_PROPOSAL, FINANCIAL_PROPOSAL, RFQ_REI, ABSTRACT_OF_BIDS, NOTICE_OF_AWARD, STATEMENT_ONGOING_CONTRACTS, SINGLE_LARGEST_COMPLETED_CONTRACT, SUPPLEMENTAL_BID_BULLETIN, NOTICE_RENEWAL_CONTRACT, REQUEST_CERT_AGENCY_TO_AGENCY, NOTICE_EXTENSION_CONTRACT, NOTICE_TERMINATE_SHOW_CAUSE, NOTICE_POST_QUAL, OFFEROR_FORM_UNSOLICITED, CONFERMENT_ORIGINAL_OFFEROR, REJECTION_UNSOLICITED_OFFER, MOA_PROCUREMENT_AGENT, MOA_POOLED_PROCUREMENT, PROCUREMENT_MONITORING_REPORT, SUSPENSION_ORDER, BLACKLISTING_ORDER, EARLY_PROCUREMENT_COMPLIANCE, OBSERVERS_REPORT, CAPACITY_DEVELOPMENT_REPORT.
- [ ] **GPPB online-form authorization:** confirm agency authorization before treating any system-generated/accomplished form as official (Res. 03-2025 caveat recorded in report).

## 5. External confirmations (production gate) [HUMAN/EXTERNAL]
- [ ] DPIA/RA 10173/NPC obligations and approvals (see `05-data-governance-training.md`).
- [ ] E-signature basis (PNPKI/RA 8792) vs wet-ink procedure.
- [ ] PhilGEPS posting/API approach.
- [ ] GPPB authorization for online accomplishment of Standard Forms (as applicable).
- [ ] Full verified legal/corpus scope for deployment.
- [ ] Real model + benchmark + human-gated training path (no auto-deploy).

## 6. Engineering follow-through (can proceed in codebase)
- [ ] Re-run `readiness_report.py` after each verified data change; keep diffs audited.
- [ ] Add/maintain tests for any rule/corpus/form parsing or registry behavior changes.
- [ ] Review server (Track C) hardening: authenticated reverse proxy/TLS for non-loopback, richer evidence viewers, runbooks — keep advisory/audited, preserve human gates.
- [ ] Extraction (Track A): only with approved/anonymized samples + local loopback runtime; plumbing check before fine-tune; frozen-slice non-regression.

## Definition of “data/config ready” (not legal sign-off)
`readiness_report` reports no fail-closed data blockers for the *checked* seeds/registry and external confirmations are documented — **still requires legal/DPO/BAC/COA/governance approvals before production use**.
