# Deployment Compliance Checklist — COA / NPC / GPPB

**Scope:** AI-assisted procurement workflow, DICT Regional Office II
**Date:** 2026-11-05
**Use:** tick each item only when the listed **evidence** exists and is stored in the transaction/audit folder. Items marked **GL** are go-live blockers.

**Legal references:** RA 12009 (New Government Procurement Act, effective 13 Aug 2024) and its IRR · GPPB Resolution No. 03-2025 (Standard Forms, 14 May 2025) · RA 9184 / 2016 IRR (legacy transactions) · RA 10173 (Data Privacy Act) · RA 8792 (E-Commerce Act) / PNPKI · COA audit and records rules.

---

## A. Legal basis and governance

| ✓ | Item | Evidence to retain | Owner |
|---|---|---|---|
| ☐ | **GL** Obtain/confirm the GPPB issuance on RA 9184 vs RA 12009 applicability during the 3-year transitory period (IRR Sec. 113; promised in GPPB Res. 03-2025) | Copy of issuance + legal officer memo | Legal / BAC Sec |
| ☐ | **GL** Confirm which regime DICT RO2 applies to new postings; document the decision | HOPE/BAC resolution or office order | HOPE |
| ☐ | **GL** Issue an internal instrument defining the system as **decision support**: AI drafts/flags/recommends; BAC/TWG/HOPE/Inspection/Accounting decide and sign | Signed office order / BAC resolution with scope, roles, override procedure | HOPE / Legal |
| ☐ | **GL** Verify legacy Annex H thresholds (Shopping/SVP) and GPPB amendments before populating the rules registry | Annotated source extract + approval of config values | Legal / BAC Sec |
| ☐ | Brief COA (or the resident auditor) on the system, the audit trail, and the verification endpoint | Minutes / acknowledgement | BAC Sec |
| ☐ | Define who may approve a ruleset change, a corpus snapshot and a model version — named humans, never an automated job | Approval matrix | HOPE |

## B. BAC and human authority

| ✓ | Item | Evidence to retain | Owner |
|---|---|---|---|
| ☐ | **GL** Every statutory checkpoint is a hard gate in the state machine, with required roles/counts configured per regime and mode | Gate configuration export + test results A7–A9 | Engineering |
| ☐ | **GL** BAC recommendations/resolutions are always editable drafts, labelled `AI_DRAFTED`, never pre-signed or auto-finalized | Document metadata + `AI_DRAFT_CREATED` / `HUMAN_EDIT_APPLIED` events | BAC Sec |
| ☐ | **GL** Signature method chosen and validated: PNPKI/RA 8792 digital signature **or** wet-ink with witnessed logged sign-off | Legal opinion + sample verification record | Legal / IT |
| ☐ | Segregation of duties enforced: drafter ≠ approver; no self-approval | Access-control policy + failed-attempt logs | IT |
| ☐ | Signoffs bound to a document hash; any post-signature edit invalidates them | Signoff schema + test A8 | Engineering |
| ☐ | Bid opening is performed and recorded by the BAC; AI only logs and pre-fills | Signed minutes + `BID_OPENING_RECORDED` event | BAC |
| ☐ | MEARB/MARB: financial envelopes remain sealed until the scheduled second opening (IRR Sec. 58.2(b)) | Two separate opening events with envelope scope | BAC |
| ☐ | E-bid passwords are never stored by the system; bidders disclose them only at opening (IRR Sec. 58.3) | Schema review + ingestion rejection test | Engineering |
| ☐ | Late bids are logged and returned unopened | Receipt log entry with disposition | BAC Sec |
| ☐ | Staff trained on: AI is advisory, how to override, how to read a citation, and what not to delegate | Attendance + training materials + quiz results | BAC Sec / HR |

## C. Procurement-rule conformance

| ✓ | Item | Evidence to retain | Owner |
|---|---|---|---|
| ☐ | **GL** All generated documents use the applicable GPPB Res. 03-2025 form (2016-era forms for legacy transactions) | Form registry with form IDs, versions, official file hashes | BAC Sec |
| ☐ | **GL** Form registry preserves the **Standard vs Recommended** classification from Res. 03-2025. RFQ, Abstract of Bids and Notice of Award are **Recommended** forms ("other forms"), explicitly *not* Standard Forms | `seed_forms.json` export (14 Standard / 22 Recommended) + legal review | Legal / BAC Sec |
| ☐ | **GL** Official form bodies downloaded, hashed and registered by a named human before any release-ready document is produced; unregistered forms emit only labelled structural drafts | `OFFICIAL_FORM_REGISTERED` events + hash manifest | BAC Sec |
| ☐ | Confirm GPPB authorization for accomplishing Standard Forms **through an online platform** (Res. 03-2025 common revision (a): "as may be authorized by the GPPB") | Written GPPB guidance or legal opinion | Legal |
| ☐ | **GL** Mode selection uses Fit-for-Purpose + thresholds (IRR Sec. 26.1), not thresholds alone; human confirms | Mode-selection record with justification | BAC |
| ☐ | All 11 RA 12009 modes (and legacy modes) have at least a stub that resolves regime, issues numbers, logs events and produces an audit folder | Mode coverage matrix | Engineering |
| ☐ | SVP configured per IRR Sec. 34: ₱2,000,000 threshold (GPPB may revise), ≤₱200,000 no posting, ≥3 invited but 1 quotation suffices, 3-calendar-day posting above ₱200k | Rules registry export | Legal / BAC Sec |
| ☐ | Posting periods from config: 7 calendar days (Sec. 50.3.1), 3 calendar days (Sec. 50.3.2); bid bulletin lead times 7/10 days (Sec. 51.4–51.5) | Timeline tracker records + proof-of-posting | BAC Sec |
| ☐ | Award criteria support LCRB, MEARB, MARB, HRRB; no auto-award to lowest quote | Evaluation configuration + sample abstract | BAC |
| ☐ | APP flow includes Indicative APP → BAC recommendation → HOPE approval → posting (IRR Sec. 7) | Signed APP + posting proof | BAC Sec |
| ☐ | Protest / request-for-reconsideration deadlines (IRR Sec. 66, 84) and security/bid-bulletin deadlines tracked | Tracker entries + alert log | BAC Sec |
| ☐ | PhilGEPS posting: confirm whether an official write API exists. If not, use posting package + human sign-off + proof capture (Sec. 50.3.3) | Written finding + procedure | IT / BAC Sec |
| ☐ | Electronic bid submission, if enabled, has the required Certification from the agency's highest IT official (Sec. 50.3.3) | Signed certification | IT |
| ☐ | Anti-tailor-fitting checks applied to TOR and RFQ specs; flags logged, never auto-rejections | Flag log + human disposition | TWG |

## D. Records, audit trail and COA readiness

| ✓ | Item | Evidence to retain | Owner |
|---|---|---|---|
| ☐ | **GL** Append-only event ledger active from the first module; `UPDATE`/`DELETE` revoked from the application role | DB grants + chain verification report | Engineering |
| ☐ | **GL** Every verdict records `model_version`, `ruleset_version`, `corpus_snapshot_id`, `prompt_hash`, retrieved passage IDs, evidence spans and citations | Sample verdict JSON | Engineering |
| ☐ | Hash chain verified hourly; daily signed manifest to WORM storage; COA-accessible verification endpoint | Verification logs + manifests | IT |
| ☐ | Per-transaction audit folder covers the full lineage (APP → bidding docs → bids → evaluation → award → contract → completion), sealed with a hash manifest at archiving | Sample COA-ready folder | BAC Sec |
| ☐ | Meta-completeness check flags missing documents across the whole lifecycle, not just one bidder's submission; flags are advisory, not legal findings | Meta-check report | Engineering |
| ☐ | **GL** Sealed folders are WORM: `UPDATE`/`DELETE` blocked by DB triggers; changed content requires a new numbered version with a recorded reason | Trigger definition + test A28 | IT / Engineering |
| ☐ | **GL** COA can verify a sealed folder read-only: folder hash, hash chain to the attested sequence, and corpus integrity | `verify_sealed_folder` report + test A29 | COA / IT |
| ☐ | COA verification endpoint is GET-only, read-only, loopback-bound; LAN access goes through an authenticated reverse proxy | Endpoint config + test A30 | IT |
| ☐ | Exported COA package (`package.json` + `MANIFEST.sha256`) is only produced after verification passes | Sample package + refusal test | BAC Sec |
| ☐ | Sealed folders survive local → VPS migration with verification still passing | Migration test A31 | IT |
| ☐ | Contract terms and milestone schedules are registered by named humans at CONTRACT stage or later | `CONTRACT_TERMS_REGISTERED` / `MILESTONES_REGISTERED` events + test A32 | Project manager / HOPE |
| ☐ | Milestone completion is human-verified with an evidence document; AI cannot certify delivery | `MILESTONE_COMPLETED` events + test A33 | Inspector / PM |
| ☐ | Delays are flagged only — no automatic liquidated-damages determination; LD rate missing produces no estimate | Sample `compute_status` output + test A34/A35 | PM / Legal |
| ☐ | Variation orders require a named approving authority other than the requester | `VARIATION_ORDER_REVIEWED` events + test A36 | HOPE |
| ☐ | Monitoring dashboard is generated locally with no external resources and an explicit advisory notice | Sample HTML + test A37 | IT / PM |
| ☐ | Monitoring records appear in the sealed COA folder and survive migration | Sample COA folder + migration test | BAC Sec / IT |
| ☐ | Numbering service: atomic per type/unit/year, gap-free, void-with-reason (a documented void is not a finding) | Numbering log + void records | BAC Sec |
| ☐ | Records retention aligned with COA/NAP schedule; backups encrypted; restore drill documented and passed | Retention policy + drill report | IT |
| ☐ | Existing paper/legacy records: defined cutover rule for what gets digitized and how authenticity is certified | Migration procedure | BAC Sec |

## E. Data privacy and security (RA 10173 / NPC)

| ✓ | Item | Evidence to retain | Owner |
|---|---|---|---|
| ☐ | **GL** Privacy Impact Assessment / DPIA completed before any real bid data is processed | Signed DPIA | DPO |
| ☐ | **GL** Lawful basis, purpose limitation, data-sharing and retention documented for bidder personal and proprietary data | Privacy notice + processing register | DPO |
| ☐ | Anonymization pipeline for historical BAC resolutions **before** they enter RAG or training data | Anonymization procedure + spot-check results | DPO / ML |
| ☐ | Confidential bid data never leaves the agency environment; **no public-cloud LLM/embedding API** at either local or VPS stage | Architecture doc + network egress rules + config review | IT |
| ☐ | Role-based access control, least privilege, MFA for approvers, and access logging (who read which bid and when) | RBAC matrix + access logs | IT |
| ☐ | NPC registration / notification obligations assessed; DPO appointed and contactable | DPO designation + NPC filing if applicable | DPO |
| ☐ | Breach response procedure covers premature bid disclosure | Incident playbook + tabletop exercise record | DPO / IT |
| ☐ | Encryption at rest and in transit; key management documented; backups encrypted | Config export + key custody record | IT |

## F. AI / model governance

| ✓ | Item | Evidence to retain | Owner |
|---|---|---|---|
| ☐ | **GL** LLM is extraction/classification only; deterministic rules engine makes pass/fail and arithmetic decisions | Architecture doc + test A10 | Engineering |
| ☐ | **GL** No AI access to bid contents before `BID_OPENING_RECORDED`; negative test passes | Test A4/A5 results + policy code | Engineering |
| ☐ | Verdict schema enforces `ABSTAIN` on low confidence, unverifiable citation or schema failure | Schema + calibration report | ML |
| ☐ | RAG retrieval is as-of the transaction date; superseded/repealed text filtered; citations verified | Test A13 + citation accuracy report | ML |
| ☐ | Frozen evaluation benchmark with per-document-type recall targets; retrieval accuracy measured separately from generation accuracy | Benchmark manifest + latest scorecard | ML |
| ☐ | Red-team set of near-miss/altered documents tested before each release | Test report | ML |
| ☐ | Model version registry: training snapshot, per-slice eval scores, human approver, deployment date, rollback target | Registry export | ML |
| ☐ | Corpus snapshot registry with human approval before activation; active transactions stay pinned | Registry export | ML |
| ☐ | Human review gate before any model/corpus promotion; shadow/canary and rollback tested | Approval record + rollback drill | ML / BAC Sec |
| ☐ | Correction logging UI captures AI said / correct answer / reason / reviewer; **second reviewer adjudicates** before an override becomes training data | Correction log + adjudication sample | BAC / ML |
| ☐ | Drafting is deterministic template fill; a 3B model never authors legal prose | Template library + code review | Engineering |
| ☐ | **GL** Extraction inference is loopback-only; any non-local AI endpoint is refused in code | `LocalHTTPBackend._assert_loopback` review + egress rules | IT / Engineering |
| ☐ | **GL** Extraction output validated against a strict typed schema; unknown keys / wrong types / hallucinated fields force `ABSTAIN`, never pass through | Schema file + validation test results | ML / Engineering |
| ☐ | Low per-field confidence and unreadable scans route to a human; an OCR failure can never produce a FAIL verdict | Abstention test results + sample verdicts | BAC / ML |
| ☐ | Every verdict records both `extraction_model_version` and `rules_engine_version`, plus an adapter-computed `prompt_hash` | Sample verdict JSON | Engineering |
| ☐ | Model server outage produces `EXTRACTION_FAILED` and **no verdict** | Failure-injection test result | Engineering |
| ☐ | **GL** Every indexed chunk carries `regime`, `effective_from/to`, `supersedes`, `source_url`, `passage_hash` and `verified`; unverified chunks can never be cited | `seed_corpus.json` / corpus table export + test A21/A22 | Legal / ML |
| ☐ | **GL** Explicit section citations resolve only to that exact section — no fuzzy substitution; failure forces `ABSTAIN` + `CITATION_MISMATCH` | Test A23 + sample verdict | ML |
| ☐ | Corpus passage integrity is hash-verifiable by COA without trusting the application | `verify_corpus_integrity()` report + test A24 | IT / COA |
| ☐ | New corpus snapshots require a named human approver; existing transactions stay pinned | `CORPUS_SNAPSHOT_ACTIVATED` events + test A25 | Legal / ML |
| ☐ | Official source PDFs retained alongside normalized local copies; the PDF remains the authoritative legal text and hashes cover only the local copy | Source archive + source_note in corpus seed | BAC Sec |

## G. Local → VPS migration

| ✓ | Item | Evidence to retain | Owner |
|---|---|---|---|
| ☐ | Identical container images on both sites; no hardcoded paths or host-specific secrets | Compose/helm files + config diff | IT |
| ☐ | Migration procedure: export → verify chain → import → re-verify → dual-read → cutover; ledger never rebuilt | Runbook + `MIGRATION_VERIFIED` event | IT |
| ☐ | Full acceptance suite (A1–A37, 85 tests) re-run on the VPS with results archived | Test report | Engineering |
| ☐ | Cutover downtime window, rollback trigger and data-owner sign-off recorded | Cutover record | HOPE / IT |
| ☐ | No external AI/embedding API enabled post-migration | Egress rules + config review | IT |

---

## Go-live gate (all must be ticked)

1. GPPB transition guidance obtained and regime decision documented (**A**)
2. Internal instrument defining AI as decision support signed (**A**)
3. Rules registry populated with verified values for both regimes (**A**, **C**)
4. Form registry active, version-pinned, Standard/Recommended classification verified, official bodies registered (**C**)
5. Custody gate and human gates pass negative tests A4–A9 (**B**, **F**)
6. Append-only ledger live, chain verifiable, COA verification endpoint available (**D**)
7. DPIA signed; no off-box inference; anonymization pipeline in place (**E**)
8. Signature method legally validated and enforced (**B**)
9. Staff trained and override procedure operational (**B**)
10. COA/DPO briefed; residual risks accepted in writing by the HOPE (**A**, **E**)

**Sign-off:** HOPE ____________ · BAC Chair ____________ · DPO ____________ · IT Head ____________ · Date __________

> This checklist is an engineering/process control aid, not a legal opinion. Have DICT legal counsel and the BAC Secretariat validate each item against the GPPB issuance current at the time of deployment.
