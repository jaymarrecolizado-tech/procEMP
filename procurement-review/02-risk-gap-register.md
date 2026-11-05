# Risk & Gap Register — AI-Assisted Procurement Workflow

**Source document:** `ai-assisted-procurement-workflow.md` (305 lines)
**Review date:** 2026-11-05
**Scope:** legal baseline, workflow design, architecture, model strategy, governance
**Companion files:** `01-revised-workflow-RA12009.md` · `03-tech-spec-rules-evidence-gate.md` · `04-compliance-checklist.md`

**Severity:** `P0` = legally or architecturally invalidating; fix before building. `P1` = material compliance/quality risk; fix before pilot. `P2` = hardening / completeness.

**Summary:** 3 × P0 · 14 × P1 · 10 × P2 = **27 findings**. Nine items are go-live blockers (marked **GL**).

---

## Findings

| ID | Sev | Area | Finding | Evidence / source | Impact | Recommended fix | Owner | Build step |
|---|---|---|---|---|---|---|---|---|
| R-01 | **P0 GL** | Legal baseline | Entire design anchored on RA 9184 / 2016 IRR, which has been repealed | IRR of RA 12009 **Sec. 115** repeals RA 9184 and CA 138; RA 12009 effective 13 Aug 2024; IRR Feb 2025 | Wrong modes, thresholds, forms, timelines, citations → audit disallowance | Re-base on RA 12009; introduce versioned rules registry with per-transaction regime | Legal / BAC Sec | 1 |
| R-02 | **P0 GL** | Legal baseline | No support for the **transitory period**; single-regime assumption | IRR **Sec. 113**: 3-year transitory period from approval of Standard Forms (GPPB Res. 03-2025, 14 May 2025 → ~May 2028); Res. 03-2025 promises further guidance on which law applies | Legacy transactions cannot be processed or reproduced correctly | Dual-regime support: `resolve_rules(as_of, regime)`, immutable regime header per transaction; obtain GPPB transition guidance | Legal / BAC Sec | 1 |
| R-03 | **P0 GL** | Stage 3 | AI OCR/extraction and "pre-check verdict" run on submitted bids **before** public bid opening | IRR **Sec. 58.1** (BAC opens after deadline), **58.3** (passwords disclosed only at opening), **58.5** (BAC ensures confidentiality), **59.1** (pass/fail check after public opening) | Premature access to sealed contents; de facto pre-evaluation; bid confidentiality breach; protest risk | Split Stage 3 into custody-only logging and post-opening ingestion; enforce `BID_OPENING_RECORDED` gate in state machine; never store e-bid passwords | BAC / Engineering | 3 |
| R-04 | **P1 GL** | Templates | All drafted documents assume 2016-era templates | GPPB **Res. 03-2025** approved new Standard Forms, effective 90 days after publication | Non-conforming bidding docs, NOA, contract, PO | Build a versioned Standard Form library; drafting = deterministic template fill | BAC Sec | 5 |
| R-05 | **P1** | Modes / Track B | "Shopping" treated as a live mode; "at least 3 canvass = mandatory" | IRR **Sec. 26.1** lists 11 modes, Shopping absent; **Sec. 34.1/34.3(c)**: SVP, ≥3 invited but **1 quotation suffices** | Wrong route, wrong rejection logic, invalid awards | Re-base Track B on SVP Sec. 34; add stubs for the other 10 modes | BAC Sec | 1, 4 |
| R-06 | **P1** | Thresholds | SVP/Shopping thresholds hardcoded implicitly; RA 12009 threshold not reflected | IRR **Sec. 34.1** ₱2,000,000 (GPPB may periodically review); **34.3(b)** ≤₱200,000 no posting; **Sec. 53.9** legacy thresholds subject to GPPB review | Wrong mode selection at planning stage | All thresholds in rules config with effective dates; verify legacy Annex H values with BAC Sec before populating | Legal | 1 |
| R-07 | **P1 GL** | Evaluation | "LCB ranking computed deterministically" is the only award logic | IRR **Sec. 34.3(g)**, **58.2**: LCRB / **MEARB / MARB / HRRB**; financial proposals remain unopened until technical scoring for MEARB/MARB | System cannot represent legal evaluation paths; risk of premature financial-envelope opening | Add award-criterion branch, technical scoring support, second opening gate for financial envelopes | BAC / Engineering | 3, 6 |
| R-08 | **P1** | Stage 0 | Mode selection described as "based on amount thresholds" | IRR **Sec. 26.1** requires **Fit-for-Purpose** approach; **Sec. 75.2.4** Fit-for-Purpose + Proportional | Misclassified procurement mode | Mode suggestion combines purpose, complexity, market, risk + thresholds; always human-confirmed | BAC | 0 |
| R-09 | **P1** | Stage 0 | APP flow skips BAC recommendation | IRR **Sec. 7** (Indicative APP → BAC recommends final APP → HOPE approves → posted) | Missing statutory step in state machine | Add BAC recommendation gate and posting of approved APP | BAC Sec | 3 |
| R-10 | **P1** | Stage 2 | Posting periods stated as "7/12 calendar days", unsourced | IRR **Sec. 50.3.1** 7 days (competitive bidding / dialogue / unsolicited offer); **50.3.2** 3 days (SVP >₱200k, limited source, two failed biddings, NGO); **Sec. 51.4–51.5** bulletin lead times | Premature opening / illegal advertisement period | All timelines from rules config; tracker emits pre-deadline warnings | BAC Sec | 1, 3 |
| R-11 | **P1** | Stage 2 | "Fully automatable — auto-posts to PhilGEPS" assumes a write API | IRR **Sec. 50.3.3** refers to "pending the mandatory implementation of the electronic bidding facility of the PhilGEPS"; requires agency IT head Certification for e-bid submission | Fragile RPA, ToS/legality risk, wrong posting has legal consequences | Posting-package generator + human content sign-off + proof-of-posting capture; automate countdown/log only | IT / BAC Sec | 3 |
| R-12 | **P1 GL** | Architecture | Audit trail compiler is build step #5 while the text says "build this early" — provenance retrofitted | IRR **Sec. 22.1 / 22.2.1**: audit trail of electronic transactions; COA may verify system integrity at any time | Early transactions have unverifiable provenance; cannot defend a challenged award | Append-only event ledger from module #1; audit folder is a read-only materialized view | Engineering | 1 |
| R-13 | **P1** | Model governance | Verdict logs only model version, not RAG corpus / retrieved passages | Self-improving loop §7 | RAG updates silently change flags; decisions not reproducible | Log `model_version` + `corpus_snapshot_id` + passage IDs + prompt hash + `ruleset_version` | Engineering | 1, 10 |
| R-14 | **P1 GL** | Model design | Fine-tuned LLM produces authoritative-looking completeness/validity verdicts | Design principle requires traceability; government context | False negatives become audit findings; untraceable "why" | LLM = extraction/classification only; deterministic rules engine decides; verdict carries evidence spans + rule citations + confidence + `ABSTAIN` | Engineering | 4 |
| R-15 | **P1** | Model design | Same 3B fine-tune proposed for drafting BAC Resolutions, contracts, NOA/NTP | Architecture diagram; build order #4 | Hallucinated legal clauses in the most sensitive documents | Deterministic Standard Form templates; LLM fills fields only; optional larger instruct model with human review | Engineering | 5, 6 |
| R-16 | **P1** | Evaluation | No abstention, no confidence threshold, no evidence spans, no red-team set, no target metrics | Document has an eval section only for the retraining loop | Silent wrong answers; no way to measure safety | Calibrated thresholds, constrained decoding, schema validation, red-team near-miss set, per-doc-type recall targets | Engineering | 4 |
| R-17 | **P1** | RAG | Retrieval is "latest corpus"; no effective dates, supersession, or as-of semantics | RAG section indexes RA 9184/IRR/RA 12009/GPPB/COA together | Wrong rule applied to a historic transaction; citation to repealed law | Chunk metadata with effective_from/to, supersedes, regime, checksum; as-of retrieval pinned per transaction; citation verification | Engineering | 7 |
| R-18 | **P1 GL** | Privacy | No Data Privacy Act treatment for bid contents, personal data, or historical resolutions | RA 10173; design indexes "anonymized historical BAC Resolutions" without an anonymization pipeline | NPC exposure; confidentiality breach | DPIA, access control, anonymization pipeline before indexing/training, retention schedule, no off-box inference | DPO / IT | 1, all |
| R-19 | **P1 GL** | Human gates | "Hard gate" asserted but no signature identity, no segregation of duties, no tamper evidence | Workflow §117; BAC docs §154 | A gate that can be self-approved is not a gate | RA 8792 / PNPKI-recognizable signatures or wet-ink + logged sign-off; drafter ≠ approver; hash-sealed gate records | IT / BAC | 3 |
| R-20 | **P1** | Governance | No agency issuance defining AI as decision-support; no COA/NPC coordination | Whole document | Over-reliance by staff; unclear accountability at audit | BAC resolution / office order defining scope, roles, override procedure; brief COA and DPO | HOPE / Legal | before pilot |
| R-21 | **P2** | Feedback loop | Every staff override becomes training data without adjudication | Self-improving loop §2 | Model learns erroneous or malicious corrections | Second-reviewer adjudication + label-quality sampling before an override enters training data | BAC / ML | 10 |
| R-22 | **P2** | Feedback loop | Gate is "meet or beat aggregate accuracy" | Self-improving loop §5 | Hides regression on rare but critical document types | Per-slice non-regression on frozen benchmark; shadow/canary; rollback trigger | ML | 10 |
| R-23 | **P2** | RAG | `bge-small-en` suggested for a corpus with Taglish text | RAG §207 | Poor retrieval on Tagalog/Taglish and section numbers | Multilingual embeddings (`bge-m3` / `multilingual-e5`) + hybrid BM25+dense | Engineering | 7 |
| R-24 | **P2** | Numbering | No void/cancellation semantics; a gap is treated as a defect | Registry detail §176 | Legitimate voided numbers become false audit findings | Void-with-reason, per-unit/per-year sequences, DB sequence under transaction, immutable log | Engineering | 2 |
| R-25 | **P2** | Coverage | Only two of eleven modes have workflows; protest/reconsideration deadlines absent | IRR **Sec. 26.1**, **Sec. 66 / 84** | Untracked modes bypass controls; missed statutory deadlines | Stubs for all modes that still resolve regime, number, log, and compile audit folder; add protest timelines to tracker | BAC Sec | 3 |
| R-26 | **P2** | Stage 9 | "Fully automatable / no approval" without immutability at archive | Workflow §82–88 | Audit package can be altered after compilation | Hash-seal folder at archiving; BAC Sec review recorded in ledger; WORM storage | IT | 8 |
| R-27 | **P2** | Forms | Design treats all GPPB forms as mandatory "Standard Forms"; no classification, and no GPPB authorization check for online accomplishment | GPPB **Res. 03-2025** pp. 3–4: only Strategic Planning Forms, PBD and PBD Related Forms are Standard Forms; "other forms" (incl. **NOA, Abstract of Bids, RFQ**) are Recommended and explicitly *not* Standard Forms; common revision (a) allows online accomplishment only "as may be authorized by the GPPB" | Misstated legal basis in BAC docs; generated forms could be treated as official without authorization | Form registry with `classification` per form; obtain GPPB authorization; block release until official bodies are hash-registered by a human | Legal / BAC Sec | 5 |

---

## Go-live blockers (GL)

R-01, R-02, R-03, R-04, R-07, R-12, R-14, R-18, R-19. None may be waived by configuration; each requires a recorded human sign-off that the fix is in place.

## Sequenced remediation (maps to revised build order)

| Step | Deliverable | Findings closed |
|---|---|---|
| 1 | Rules registry + regime resolver + event ledger | R-01, R-02, R-06, R-10, R-12, R-13, R-18 (partial) |
| 2 | Numbering service with void semantics | R-24 |
| 3 | State machine: custody gate, human gates, timelines, mode stubs | R-03, R-07 (gate), R-09, R-11, R-19, R-25 |
| 4 | Extraction-only checker + rules engine + abstention | R-14, R-16 |
| 5 | GPPB Res. 03-2025 form registry (Standard + Recommended), official-body hash registration | R-04, R-27 |
| 6 | Field-fill drafting assistant | R-15 |
| 7 | As-of RAG with citation verification | R-05 (rules), R-08, R-17, R-23 |
| 8 | Audit folder compiler (read-only view) | R-26 |
| 9 | Dashboards | — |
| 10 | Correction pipeline + retraining | R-21, R-22 |
| Cross-cutting | Governance/issuance, DPIA, training | R-18, R-20 |

## Residual risks after remediation

- GPPB transition guidance may change the dual-regime rule; the registry is designed to absorb this without code changes.
- PhilGEPS posting/e-bidding interface availability is external; Stage 2 remains human-confirmed until an official API exists.
- LLM extraction accuracy on poor-quality scans remains a measured, human-verified risk — never a fully automated determination.
