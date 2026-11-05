# AI-Assisted Procurement Workflow — Revised Design (Dual-Regime)

**Status:** Draft for BAC Secretariat / legal review
**Date:** 2026-11-05
**Supersedes:** `ai-assisted-procurement-workflow.md` (RA 9184 / 2016 IRR baseline)
**Target deployment:** DICT Regional Office II · local workstation first, then agency VPS
**Legal sources verified:** IRR of RA 12009 (GPPB, Feb 2025), GPPB Resolution No. 03-2025 (14 May 2025), 2016 Revised IRR of RA 9184 (legacy, via GPPB/COA references)
**Build status (2026-11-06):** steps 1–8 implemented in `procurement-core/` (stdlib-only prototype, 72 passing tests). Steps 9–10 pending. No model weights or embedding models are bundled; the Qwen2.5-3B fine-tune plugs in behind `extraction.LocalHTTPBackend`.

> **Read this first.** The previous design is structurally sound but legally stale. RA 12009 took effect 13 August 2024; Section 115 of its IRR **repeals RA 9184 and Commonwealth Act No. 138**. Section 113 keeps RA 9184 in force before the IRR's effectivity and grants a **three-year transitory period from GPPB approval of the Standard Forms** (GPPB Res. 03-2025, 14 May 2025 → approximately May 2028). GPPB Res. 03-2025 also states that a subsequent issuance will clarify which law applies during the transition. Until that guidance is obtained, this design supports **both regimes** rather than guessing.

---

## 0. Design principles (revised)

1. **AI assists; named human bodies decide.** BAC, TWG, HOPE, Inspection Committee and Accounting retain every statutory act. AI output is always a labelled draft or recommendation.
2. **No AI access to bid contents before public opening.** This is a hard system boundary, not a policy caveat. (See §3, Stage 3.)
3. **Rules live in data, not in weights.** Thresholds, timelines, modes and forms are versioned configuration, interpreted as-of the transaction date. Fine-tuning teaches task behaviour only.
4. **Every assertion carries evidence.** A verdict cites the rule version, the section, the retrieved passage, and the document page/field it came from.
5. **The audit trail is a day-one primitive.** An append-only event ledger starts with the first module, not as a later compiler.
6. **Local-first, portable.** No third-party LLM API at any stage. Containerized so the same stack runs on a workstation and on the VPS.
7. **Fail closed.** Low confidence, missing rule, missing signature or unverifiable citation routes to a human; the system never silently guesses.

---

## 1. Legal baseline and regime selection

### 1.1 What actually applies

| Item | RA 9184 / 2016 IRR (legacy) | RA 12009 / IRR (current) |
|---|---|---|
| Status | Repealed by IRR Sec. 115; still relevant to transactions begun before the IRR took effect and during the transitory period | In force; IRR effective 15 calendar days after publication (Sec. 117) |
| Modes | Competitive Bidding, Limited Source Bidding, Direct Contracting, Repeat Order, **Shopping**, Small Value Procurement, Negotiated Procurement | Sec. 26.1: Competitive Bidding, Limited Source Bidding, **Competitive Dialogue**, **Unsolicited Offer with Bid Matching**, Direct Contracting, **Direct Acquisition**, Repeat Order, Small Value Procurement, Negotiated Procurement, **Direct Sales**, **Direct Procurement for Science, Technology and Innovation** |
| Small-value route | Shopping / SVP under Sec. 52 / 53.9, thresholds in Annex H as amended by GPPB | **SVP Sec. 34**: threshold ₱2,000,000 (subject to GPPB periodic review); ≤₱200,000 no posting; ≥3 suppliers invited, 1 quotation sufficient to proceed |
| Award criteria | LCRB / lowest calculated responsive quotation (also HRRB for consulting) | **LCRB, MEARB, MARB, HRRB** (Sec. 34.3(g), Sec. 58.2) |
| Posting | Varies by mode and ABC | Sec. 50.3.1: 7 calendar days (competitive bidding, competitive dialogue, unsolicited offer); Sec. 50.3.2: 3 calendar days (SVP above ₱200k, limited source, two failed biddings, NGO participation) |
| Bid opening | Public opening; pass/fail eligibility check | Sec. 58.1 public opening; Sec. 58.2(b) financial proposals stay sealed for MEARB/MARB; Sec. 58.3 e-bid passwords disclosed only at opening; Sec. 58.5 BAC ensures integrity/security/confidentiality; Sec. 59.1 pass/fail check **after** public opening |
| Forms | 2016-era Philippine Bidding Documents and agency templates | **GPPB Res. 03-2025 forms**, effective 90 days after publication (targeted end-June 2025); see §1.2 for the mandatory vs recommended distinction |

> **Open item (owner: BAC Secretariat / legal):** obtain the GPPB issuance promised in Res. 03-2025 on the applicability of RA 9184 vs RA 12009 during the transitory period. Confirm exact legacy SVP/Shopping thresholds from 2016 IRR Annex H and its GPPB amendments. Do not hardcode either value until confirmed.

### 1.2 Form classification — do not call every form a "Standard Form"

GPPB Res. 03-2025 draws an explicit line that the earlier version of this design glossed over:

- **Standard Forms (mandatory use):** Strategic Planning Forms (APP, PPMP, Mandatory Market Scoping Form), the Philippine Bidding Documents for Goods / Infrastructure / Consulting, and PBD Related Forms (Contract Form, Omnibus Sworn Statement, Bid Securing Declaration, Performance Securing Declaration, Bid Form, Price Schedule, Technical Proposal Forms, Financial Proposal Forms). Res. 03-2025 states that "upon issuance of the GPPB, the use of which shall be mandatory for all PEs".
- **Recommended Forms ("other forms", explicitly *not* classified as Standard Forms):** Request for Quotation / Request for Expression of Interest, Abstract of Bids, **Notice of Award**, Supplemental Bid Bulletin, Notice of Post Qualification with Additional Requirements, Procurement Monitoring Report, Observer's Report, and the rest of the 22-form list. These are templates to guide PEs, not prescribed mandatory forms.

Consequences for the build: the form registry must carry `classification` per form; RFQ, Abstract of Bids and NOA are drafted from the *Recommended* set. Do not describe them as mandatory Standard Forms in the compliance checklist or in BAC documentation. The prototype implements this in `procurement-core/seed_forms.json` (36 forms, 14 Standard / 22 Recommended).

**Additional open item:** Res. 03-2025's common revision (a) allows Standard Forms to be accomplished "either manually or through online platforms, **as may be authorized by the GPPB**". Confirm that a system-generated form counts as an accomplished form for DICT RO2 before treating any generated document as official. Until then, generated output is a labelled structural draft that cannot be released.

### 1.3 Regime resolution rule

```
regime = resolve_rules(
    as_of      = transaction.posting_date OR transaction.created_date,
    origin     = transaction.legacy_reference (optional),
    override   = legal_officer_approval (recorded, reason required)
)
```

- The resolved `regime` + `ruleset_version` + `corpus_snapshot_id` are written **once** into the transaction header and are immutable thereafter.
- Re-resolving rules on a later date is forbidden for an existing transaction — this is what makes a two-year-old decision reproducible at audit time.
- A transaction that spans the transition (posted under one regime, awarded under another) must be flagged for legal-officer decision, never auto-migrated.

---

## 2. Shared platform modules

```
                    ┌──────────────────────────────────────────────┐
                    │  Procurement Orchestration (state machine)    │
                    │  stage · regime · gates · statutory timers    │
                    └───────┬──────────────────────────────────────┘
                            │
   ┌──────────┬─────────────┼────────────┬──────────────┬─────────────┐
   ▼          ▼             ▼            ▼              ▼             ▼
Rules      Event        Document     Drafting       As-of RAG     Numbering
Registry   Ledger       Checker      (templates)    Knowledge     Service
(versioned (append-only (extraction  (deterministic Base          (atomic,
 law data) hash chain)  only)        fill)         (citations)   logged)
   │          │             │            │              │             │
   └──────────┴─────────────┴────────────┴──────────────┴─────────────┘
                            │
                   Audit Folder Compiler (view over ledger)
                   Monitoring Dashboards · Correction Pipeline
```

**Module contracts**

| Module | Responsibility | Must NOT do |
|---|---|---|
| Rules registry | Versioned modes, thresholds, timelines, required-document checklists, form IDs, effective dates | Contain model logic |
| Event ledger | Append-only record of every document, access, verdict, override, citation, sign-off, model+corpus version | Allow update/delete |
| Numbering service | Atomic PR#/RFQ#/Resolution#/PO#/Contract# per type per unit per year; void-with-reason | Reuse or silently skip numbers |
| Document checker | OCR → field extraction → classification; confidence + evidence spans | Decide compliance |
| Rules engine | Deterministic pass/fail over extracted fields against the applicable ruleset | Depend on LLM output for arithmetic or presence checks |
| Drafting assistant | Fill registered GPPB form templates with approved data; cite source of each field | Invent clauses, pre-sign, or finalize |
| As-of RAG | Retrieve the rule text in force on the transaction date; return section + passage + version | Return "latest" by default |
| Audit compiler | Read-only materialized folder per transaction | Be the system of record |

---

## 3. Track A — Competitive bidding (Stages 0–10)

| Stage | AI role | Human authority | Hard gate |
|---|---|---|---|
| 0 Planning | Consolidate PPMP into Indicative APP; flag budget/line-item mismatches; **suggest modes using Fit-for-Purpose + thresholds**, not thresholds alone | BAC recommends final APP; HOPE approves (Sec. 7). Posting of approved APP | Yes |
| 1 Bidding docs | Draft from the applicable **GPPB Res. 03-2025 forms** (Standard / Recommended per §1.2); anti-tailor-fitting scan; generate required-document checklist bound to `ruleset_version` | BAC + TWG approve | Yes |
| 2 Advertisement | Generate posting package, compute deadlines, log proof of posting (7 calendar days, Sec. 50.3.1) | BAC Secretariat signs content; **automate countdown only** | Yes for content |
| 3 Receipt / custody | **Outer-envelope logging only**: bidder, timestamp, envelope count, seal condition, late/early flag. No OCR, no content read, no password storage | BAC Secretariat custody | Yes |
| 4 Public opening | Pre-fill Abstract of Bids from data entered **during** the opening; flag discrepancies live | BAC opens and reads bids; signed Abstract (Sec. 58) | Yes — **unlocks content ingestion** |
| 5 Extraction | After the opening event is recorded: OCR, extraction, pass/fail checklist (Sec. 59.1), evidence spans, abstentions | BAC verifies | Advisory |
| 6 Evaluation | Deterministic arithmetic for LCRB; **for MEARB/MARB, only technical scoring support — financial envelopes remain sealed until the BAC's scheduled second opening (Sec. 58.2(b))**; post-qualification cross-check | TWG scores, BAC deliberates and votes | Yes |
| 7 Award docs | Draft BAC recommendation, NOA, NTP, Contract from approved outcome; consistency check vs bidding docs | BAC signs recommendation/resolution; HOPE signs | Yes — non-negotiable |
| 8 Implementation | Milestone tracking, delay/LD/variation-order flags, status reports | Project manager / TWG decides | Advisory |
| 9 Completion & payment | Completion-document validation, invoice vs contract/delivered quantity cross-check | Inspection Committee / Accounting certify | Yes |
| 10 Records | Compile COA-ready folder from the ledger; meta-completeness check; WORM seal | BAC Secretariat reviews and seals; COA verifies read-only | Review + seal |

**Stage 3/4 boundary (the single most important control):** no component — OCR service, vector index, model, dashboard, backup job — may read or index bid contents until the `BID_OPENING_RECORDED` event exists for that transaction. For electronic bids, the system never holds bidder passwords (Sec. 58.3). Financial envelopes for MEARB/MARB get a second, separately recorded opening gate. Late bids are logged and returned unopened.

**BAC documents:** AI drafts only, presented as an editable draft with an `AI_DRAFTED` label; log AI draft vs human edits; Chair and members sign. Registry of resolution numbers maintained automatically with void-with-reason.

---

## 4. Track B — Alternative modes

### 4.1 SVP (replaces the old "Shopping / SVP" track)

`Purchase Request → RFQ / RFP → Abstract of Quotations → BAC recommendation → PO / Contract`

| Step | Current rule (RA 12009 IRR) | AI role | Human gate |
|---|---|---|---|
| PR | Item must be in approved APP; ABC determines route | Draft PR from free text; validate against APP; flag if ABC exceeds SVP threshold or mode is misclassified | End-user, Budget Officer, HOPE |
| RFQ | Sec. 34.3(b): ≤₱200,000 no posting; above that, 3 calendar days on PhilGEPS/agency site/premises (Sec. 50.3.2) | Generate RFQ from the registered **Recommended** form (not a Standard Form), apply anti-tailor-fitting check, log invitees and timestamps | BAC/Canvasser confirms invitee list |
| Quotations | Sec. 34.3(c): send to ≥3 qualified suppliers; **1 quotation is sufficient to proceed** | Extract quotations into structured comparison table; never auto-reject | — |
| Abstract | Names of respondents + quotations/ratings (Sec. 34.3(f)) | Build abstract; flag quotes near ABC or suspiciously identical formatting (flag only) | BAC reviews and signs |
| Award | Sec. 34.3(g): LCRB / MEARB / MARB / HRRB — **not automatically the lowest quote** | Draft recommendation from evaluation data | BAC recommends; HOPE approves and signs |
| PO | Total validated against ABC / PR amount | Draft PO, auto-fill supplier details | HOPE / authorized signatory |

> Legacy-regime transactions keep the 2016 IRR Shopping/SVP logic, but it is selected from the rules registry — never from a separate codebase.

### 4.2 Other modes (stubs in v1, full flows later)

Competitive Dialogue · Unsolicited Offer with Bid Matching · Limited Source Bidding · Direct Contracting · Direct Acquisition · Repeat Order · Negotiated Procurement (all instances) · Direct Sales · Direct Procurement for STI. Each stub must still: resolve a regime, obtain a number, log every document and sign-off, and produce an audit folder. **A mode without a full workflow is not an excuse to bypass the ledger.**

Also add to the timeline tracker: protest and request-for-reconsideration deadlines (IRR Sec. 66, 84), bid-security and performance-security deadlines, and Supplemental Bid Bulletin lead times (Sec. 51.4–51.5: 7 calendar days; clarification requests 10 days before deadline).

---

## 5. As-of RAG layer

**Corpus:** RA 12009 + IRR (chunked by section), GPPB Res. 03-2025 Standard Forms, GPPB resolutions/circulars, COA circulars, RA 9184 + 2016 IRR + Annex H (legacy corpus), DICT RO2 internal procurement manual, anonymized historical BAC decisions, PhilGEPS eligibility references.

**Every chunk carries:** `law`, `section`, `issuance_no`, `effective_from`, `effective_to`, `supersedes`, `regime`, `source_url`, `checksum`.

**Retrieval:** hybrid BM25 + dense (section numbers and citation IDs matter). Multilingual embeddings (`bge-m3` or `multilingual-e5`) for Taglish; avoid `bge-small-en`. Query is constrained by `as_of = transaction.posting_date` and `regime`; superseded chunks are never returned unless explicitly requested for historical explanation.

**Citation verification:** before a flag is shown, check that the retrieved passage actually supports the asserted rule. Log `corpus_snapshot_id` + passage IDs with every verdict. Evaluate retrieval accuracy separately from generation accuracy.

**Change management:** a monitored ingestion job for new GPPB issuances; new corpus version requires human approval before activation; active transactions keep their pinned snapshot.

**Implemented in `procurement-core/rag.py` + `seed_corpus.json` (step 7):**
- 13 verbatim passages from the official IRR of RA 12009 and GPPB Res. 03-2025, chunked by section with `regime`, `effective_from/to`, `supersedes`, `source_url`, `passage_hash` and `verified` metadata. One clearly-labelled legacy placeholder chunk is `verified=false` and can never be cited. Passage hashes cover the **local normalized copy**, not the official PDF — the PDF remains authoritative.
- `AsOfRAG.retrieve()` filters by the transaction's **pinned** snapshot, regime and effective dates; excludes superseded chunks; BM25 + exact section-number boost, with an optional pluggable dense backend (no embedding model bundled — `StubEmbedding` is wiring-only).
- **An explicit section citation must resolve to that exact section.** Fuzzy fallback is refused when a section number is present, so `Sec. 999.9` can never be "verified" by a similar-looking rule. An unverifiable citation yields `verified=false` → the checker's verdict is forced to `ABSTAIN` + a `CITATION_MISMATCH` event.
- `verify()` re-checks snapshot pinning, regime, effective dates, supersession and passage hash; `verify_corpus_integrity()` recomputes all hashes for COA. `build_snapshot()` / `activate_snapshot()` require a named human approver, and existing transactions keep their pinned snapshot.

---

## 6. Model strategy

- **Qwen2.5-3B fine-tune** → extraction, field classification, document-type detection **only**. Constrained JSON decoding with strict post-hoc schema validation. The adapter contract (`extraction.py`, `seed_extraction_schema.json`) requires typed fields, per-field confidence and page-level evidence; unknown document keys or hallucinated fields are schema violations that force `ABSTAIN`, never extra data. `LocalHTTPBackend` **refuses any non-loopback URL**, so bid data cannot leave the agency machine. The adapter — not the model — computes `prompt_hash`, so a model response cannot rewrite its own provenance.

| Extraction outcome | System behaviour |
|---|---|
| Clean typed fields, confidence ≥ threshold | Passed to the deterministic checker → PASS / FAIL |
| Low per-field confidence | Field omitted → checker abstains on that field |
| Unreadable scan / OCR failure | `ABSTAIN`, routed to a human — never a disqualification |
| Schema violation, wrong type, hallucinated field, malformed JSON | `ABSTAIN` verdict + `SCHEMA_VALIDATION_FAILED` event |
| Model server unreachable | `EXTRACTION_FAILED` event, **no verdict produced** |
| Extraction attempted before public bid opening | Blocked by the custody gate |
- **Rules engine** → all presence checks, date/expiry validity, arithmetic, ranking, threshold comparisons. No LLM in this path.
- **Drafting** → deterministic registered form templates (Standard and Recommended classified separately); LLM fills fields from approved data, never writes legal prose. A 3B model must not author BAC resolutions or contract clauses. Official form bodies must be hashed and registered by a human before a release-ready document can be produced.
- **Verdict schema:** `status`, `missing_documents[]`, `invalid_documents[]`, `notes[]`, `evidence_spans[]`, `rule_citations[]`, `confidence`, `decision = pass | fail | ABSTAIN`, `model_version`, `ruleset_version`, `corpus_snapshot_id`.
- **Abstention:** below calibrated threshold, or on citation mismatch, output `ABSTAIN` and route to a human. No silent best-guess.
- **Evaluation:** per-document-type **recall on missing/invalid detection** (false negatives are the audit risk), extraction F1, citation accuracy, retrieval accuracy, hallucination rate, and a red-team set of near-miss/altered documents. Frozen benchmark set, never used for training.
- **Language:** test explicitly on Taglish and scanned/low-quality documents.

---

## 7. Self-improving loop (human-gated)

1. Production model + pinned corpus serve verdicts.
2. Correction UI captures override: AI said / correct answer / reason / reviewer. **Second reviewer adjudicates** before an override can become training data — staff corrections can be wrong or gamed.
3. Periodic retraining batch combines corrections with the original training set (no catastrophic forgetting).
4. Candidate evaluated on the frozen benchmark with **per-slice non-regression** (a single aggregate accuracy gate hides regressions on rare but critical document types).
5. **Human review gate** approves promotion.
6. Versioned deployment: old model and corpus snapshot retained for rollback; shadow/canary before full cutover.
7. Every verdict logs model version **and** corpus snapshot and retrieved passage IDs — a RAG update changes answers just as much as a new model.

Registries required: model version table (training snapshot, eval scores per slice, approver, date), corpus version table, correction log, benchmark manifest.

---

## 8. Deployment: local → VPS

- Containerized services (API, DB, vector store, model runtime, worker). No external model/embedding API. All inference local on the workstation, later on the VPS.
- PostgreSQL for transactional data and the event ledger; Qdrant/Chroma for vectors; object storage for documents with checksums.
- Migration must preserve the hash chain: export → verify chain → import → re-verify → dual-read period → cutover. Never rebuild the ledger from scratch.
- Backups encrypted, with documented restore drills; retention per COA/NAP schedule; least-privilege access; separation of duties (drafter ≠ approver).
- Data Privacy Act (RA 10173): DPIA before processing real bids, anonymize historical resolutions before indexing, no confidential bid sent off-box, access logs retained.

---

## 9. Revised build order

| # | Build | Status | Why this position |
|---|---|---|---|
| 1 | **Rules registry + event ledger + regime resolver** | Done | Every other module needs versioned rules and an immutable log; retrofitting provenance is impossible |
| 2 | **Numbering service** | Done | Cheap, prevents recurring audit findings, everything depends on it (keep from original plan) |
| 3 | **State machine with the sealed-bid custody gate** | Done | Encodes the highest legal risk as a technical impossibility, not a policy |
| 4 | **Deterministic checker + rules engine** | Done | Core verdict engine; advisory, evidence-backed, abstaining |
| 5 | **Form registry + template library / field-fill drafting** | Done | Legal text must be deterministic; official bodies hash-registered by a human |
| 6 | **OCR / extraction adapter (Qwen2.5-3B plug-in)** | Done | Model is a field extractor only; strict schema validation and loopback-only inference keep it inside the compliance boundary |
| 7 | **As-of RAG + citation verification** | Done | Adds traceability; seeded with verified official passages, pinned per transaction, exact-section citation check |
| 8 | **COA packaging, WORM sealing, read-only verification endpoint** | Done | Immutable sealed folders, meta-completeness flags, hash-chain attestation, CLI + loopback HTTP verification |
| 9 | **Monitoring dashboards** | Next | Stage 8 / project tracker integration |
| 10 | **Correction logging + retraining pipeline** | Pending | Last — needs real production corrections to be meaningful |

**COA packaging (`procurement-core/audit.py`, `coa_server.py`):** `compile_folder()` produces a read-only snapshot covering the transaction, every ledger event with hashes, documents and checksums, signoffs, verdicts, drafts, rule citations, corpus/model versions, the ledger-chain state it attests to, and a **stage-aware meta-completeness** check (flags are advisory, not legal findings). `seal_folder()` writes an immutable sealed row (DB triggers block UPDATE/DELETE) plus a `FOLDER_SEALED` event. Re-sealing unchanged content is idempotent; changed content requires a **new numbered version with a recorded reason**, and the old version is retained. `verify_sealed_folder()` recomputes the folder hash, replays the hash chain up to the attested sequence, and checks corpus integrity — all reads, no writes. `coa_server.py` exposes GET-only `/health`, `/verify-chain`, `/verify-folder`, `/folder`, `/integrity`, `/list-seals` on loopback, plus offline CLI commands for chain/folder verification, export and sealing. Production deployment should place an authenticated reverse proxy in front if COA needs LAN access.

---

## 10. Assumptions and open questions

1. **Assumed:** design/prototype stage, no module in production. If any module is live, treat rows P0 in the gap register as migration items with re-validation.
2. **Assumed:** local-first deployment with later migration to an agency VPS; no public cloud LLM APIs.
3. **Open:** GPPB guidance on RA 9184 vs RA 12009 applicability during the transitory period.
4. **Open:** exact legacy Annex H thresholds and their GPPB amendments — populate the rules registry only after BAC Secretariat confirmation.
5. **Open:** whether PhilGEPS offers a supported write API for posting. Until confirmed, Stage 2 generates a posting package and captures proof manually.
6. **Open:** whether DICT RO2 has an internal procurement manual and an existing BAC resolution/logbook to import.
7. **Open:** e-signature acceptance (PNPKI / RA 8792) for hard gates, or wet-ink with scanned logged sign-off.
8. **Open:** GPPB authorization for accomplishing Standard Forms through an online platform (Res. 03-2025 common revision (a)) — required before a system-generated form can be treated as an accomplished form.
9. **Open:** official form bodies (PDF/DOCX) for all 36 Res. 03-2025 forms must be downloaded, hashed and registered by a named human; until then the system emits only labelled structural drafts.
