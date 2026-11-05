# AI-Assisted Full Procurement Workflow (RA 9184 / 2016 IRR)

## Design Principle

The AI model (your fine-tuned Qwen2.5-3B + supporting tools) **assists and accelerates** every stage of the procurement cycle, but **does not replace the BAC (Bids and Awards Committee) or approving authority** at legally/decision-critical checkpoints. This keeps the system useful and fast while staying compliant — RA 9184 assigns specific statutory responsibilities to named human bodies (BAC, TWG, HOPE/Head of Procuring Entity), and a government system that lets AI make or silently finalize those calls would create real legal exposure. So: **AI drafts, flags, and recommends at every stage; humans approve at every stage that RA 9184 assigns to a named body.**

---

## Stage-by-Stage Workflow

### Stage 0 — Procurement Planning
**Human-led, AI-assisted**
- Input: APP (Annual Procurement Plan) items, budget, PPMP from end-user units
- AI role:
  - Drafts/validates PPMP consolidation into APP format
  - Flags budget/line-item mismatches against COA/DBM circulars
  - Suggests procurement mode (public bidding vs. alternative methods) based on amount thresholds under RA 9184 IRR
- Output: Draft APP for BAC Secretariat review → **Human approval by Head of Procuring Entity (HOPE)**

### Stage 1 — Pre-Procurement / Bidding Documents Preparation
**AI-heavy drafting, human sign-off**
- AI role:
  - Auto-generates draft Bidding Documents (ITB, TOR, eligibility requirements) from APP line item + templates
  - Cross-checks TOR specs against anti-"tailor-fitting" red flags (overly specific brand/spec language that narrows competition — a common audit finding)
  - Drafts the required documents checklist specific to this procurement (this is where your fine-tuned model's schema plugs in)
- Output: Draft bidding docs → **Human approval: BAC + TWG**

### Stage 2 — Advertisement & Posting
**Fully automatable**
- AI role:
  - Auto-posts to PhilGEPS, agency website, physical posting log
  - Tracks mandatory posting periods (7/12/calendar days depending on method and value) and flags premature opening
- Output: Posting confirmation + automated countdown to bid opening — **no human approval needed, informational only**

### Stage 3 — Bid Submission & Receipt
**AI intake, human custody**
- AI role:
  - OCR/extraction pipeline (from your earlier plan) ingests submitted bid documents
  - Deterministic rules engine checks completeness against Stage 1's generated checklist
  - Fine-tuned model produces the completeness/validity verdict JSON per bidder
- Output: Per-bidder eligibility pre-check report → **Human verification: BAC Secretariat / TWG confirms before bid opening**
- **Critical control**: AI pre-checks are advisory only until the actual bid opening event — RA 9184 requires bids to be publicly opened and read, so the AI's completeness check must not be mistaken for the official eligibility determination.

### Stage 4 — Bid Opening
**Human event, AI-logged**
- AI role:
  - Generates the Abstract of Bids form pre-filled from extracted data
  - Flags discrepancies live (e.g., bid amount on envelope vs. financial proposal) for the BAC to resolve on the spot
- Output: Signed Abstract of Bids (human signatures required by law) — **BAC performs the actual opening/reading**

### Stage 5 — Eligibility Check, Bid Evaluation, Post-Qualification
**This is where your fine-tuned model does the most work**
- AI role:
  - Full completeness + validity verdict per your trained schema (`status`, `missing_documents`, `invalid_documents`, `notes`)
  - Lowest Calculated Bid (LCB) ranking computed deterministically (not by the LLM — straight arithmetic/rules)
  - Post-qualification checklist cross-check (site visit requirements, similar-contract track record verification against submitted docs)
  - Drafts the BAC Resolution recommending LCRB (Lowest Calculated Responsive Bid)
- Output: Draft BAC Resolution + supporting verdict reports → **Human decision: BAC deliberates and votes; AI output is a recommendation, never the resolution of record**

### Stage 6 — Award & Contract Preparation
**AI drafting, human authority**
- AI role:
  - Drafts Notice of Award, Notice to Proceed, and Contract based on BAC Resolution outcome
  - Cross-checks contract terms against the original bidding documents for consistency (catches copy-paste errors between TOR and contract)
  - Tracks statutory timelines (e.g., performance security submission deadlines)
- Output: Draft NOA/NTP/Contract → **Human approval: HOPE signs**

### Stage 7 — Contract Implementation & Monitoring
**AI monitoring, human management**
- AI role:
  - Tracks delivery/completion milestones vs. contract schedule
  - Flags delays, liquidated damages triggers, variation order requests needing review
  - Generates status reports for program managers (ties into your "DICT project tracker" idea from earlier)
- Output: Ongoing dashboards and alerts — **Project manager/TWG makes implementation decisions**

### Stage 8 — Completion, Acceptance, Payment
**AI pre-check, human certification**
- AI role:
  - Validates completion documents (inspection report, acceptance certificate, warranty docs) against the same completeness/validity logic as Stage 5
  - Cross-checks invoiced amounts against contract and delivered quantities
- Output: Pre-payment validation report — **Human: Inspection Committee / Accounting certifies for payment**

### Stage 9 — Records, Audit Trail & COA Readiness
**Fully automatable**
- AI role:
  - Auto-compiles the full procurement folder (APP → bidding docs → bids → evaluation → award → contract → completion) into a single audit-ready package per transaction
  - Flags missing documents in the audit trail itself (meta-completeness check across the whole lifecycle, not just one bidder's submission)
- Output: COA-ready procurement folder — **no approval needed, but BAC Secretariat reviews before archiving**

---

## System Architecture Implication

This means your earlier fine-tuned model isn't a single-purpose document checker anymore — it becomes **one module in a larger procurement orchestration system**:

```
┌─────────────────────────────────────────────────────────┐
│              Procurement Orchestration Layer              │
│  (workflow state machine: tracks which stage each         │
│   procurement transaction is in, routes to the right       │
│   human approver, enforces statutory timelines)            │
└─────────────────────────────────────────────────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
  ┌──────────┐  ┌──────────────┐ ┌───────────┐ ┌──────────────┐
  │ Document │  │ Doc Drafting │ │ Timeline/ │ │ Audit Trail  │
  │ Checker  │  │ (bidding docs,│ │ Deadline  │ │ Compiler     │
  │ (your    │  │ NOA, contract │ │ Tracker   │ │              │
  │ Qwen 3B  │  │ drafts — can  │ │ (pure     │ │ (pure logic, │
  │ fine-tune)│  │ reuse same    │ │ logic,    │ │ no model     │
  │          │  │ fine-tune or  │ │ no model  │ │ needed)      │
  │          │  │ a template    │ │ needed)   │ │              │
  │          │  │ engine)       │ │           │ │              │
  └──────────┘  └──────────────┘ └───────────┘ └──────────────┘
```

Each human-approval checkpoint in the stages above should be a **hard gate** in the state machine — the system should not let a transaction move to the next stage without the required human sign-off recorded, regardless of what the AI recommended.

---

## Document Management Layer — PR, RFQ, BAC Docs

Public bidding (Stages 0–9 above) is only one procurement track. A large share of actual DICT RO2 transactions will go through **Shopping / Small Value Procurement** (RA 9184 IRR, Alternative Methods) instead, which runs on a shorter document set: **Purchase Request (PR) → Request for Quotation (RFQ) → canvass/comparison → BAC Resolution → Purchase Order (PO)**. This needs its own managed document flow, parallel to the public bidding track above, sharing the same checker/drafting/audit modules.

### PR (Purchase Request)
- **Origin**: end-user unit (e.g., a division needing laptops, supplies, a service)
- AI role:
  - Drafts PR from a free-text request ("we need 5 units of X for Y purpose") into the standard PR form fields (item description, quantity, ABC/estimated cost, account code, purpose/justification)
  - Validates PR against APP — flags if the item/amount isn't in the approved APP (a common audit finding) or if ABC exceeds small-value/shopping thresholds and should route to public bidding instead
  - Auto-assigns PR control number and routes for signature
- **Human gate**: End-user signs request; Budget Officer certifies fund availability; HOPE/authorized official approves PR

### RFQ (Request for Quotation)
- **Trigger**: approved PR under shopping/small-value threshold
- AI role:
  - Auto-generates RFQ from the PR's item specs, applying your TOR/spec red-flag check (same anti-tailor-fitting logic as Stage 1) so RFQ specs aren't unintentionally written to favor one supplier
  - Sends/tracks RFQ to the minimum required number of suppliers (RA 9184 requires canvassing at least 3 for shopping), logs who was invited and when
  - Receives and extracts supplier quotations (reusing your document-checker extraction pipeline) into a structured comparison table
- **Human gate**: BAC/Canvasser signs off on which suppliers were canvassed; none needed for the RFQ issuance itself if it strictly follows the approved PR

### Canvass / Abstract of Quotations
- AI role:
  - Auto-builds the Abstract of Canvass: lowest quote identification, compliance check per supplier (business permit, PhilGEPS registration validity — same validity-check logic from your document checker), completeness of each quotation
  - Flags any quote that's suspiciously close to the ABC or shows signs of pre-arranged bidding (identical formatting/pricing across "competing" suppliers — worth flagging, not auto-rejecting)
- **Human gate**: BAC reviews and signs the Abstract of Quotations

### BAC Docs (Resolutions, Minutes, Recommendations)
This is the document type that needs the most careful human-AI boundary, since BAC Resolutions are the actual legal record of a procurement decision.
- AI role:
  - Drafts the BAC Resolution text (recommendation, lowest calculated/responsive quote or bid, justification) from the evaluation data already produced upstream — this is drafting, not deciding
  - Drafts BAC meeting minutes from either a structured agenda template or a transcript/notes input (if you record BAC deliberations), formatted to the standard minutes layout
  - Cross-checks resolution numbering sequence and ensures required attachments (Abstract of Bids/Quotations, eligibility checks) are referenced and attached before the resolution is finalized
  - Maintains the BAC Resolution registry/logbook automatically (number, date, subject, linked PR/RFQ/bid reference) for audit lookup
- **Human gate (hard, non-negotiable)**: The BAC Chair and members deliberate and sign the Resolution themselves. The system should present the AI-drafted resolution as an editable draft, never as a pre-signed or auto-finalized document, and should log that it was AI-drafted vs. human-edited for audit transparency.

### Purchase Order (PO) / Notice of Award for shopping track
- AI role: drafts PO from the approved BAC Resolution + winning quotation, auto-fills supplier details, validates total against ABC/PR amount
- **Human gate**: HOPE/authorized signatory signs PO

### How this plugs into the architecture

```
PR ──▶ RFQ ──▶ Canvass/Abstract ──▶ BAC Resolution ──▶ PO
 │        │            │                   │             │
 └────────┴────────────┴───────────────────┴─────────────┘
                        │
              All route through the SAME modules:
     Document Checker (completeness/validity) · Drafting Assistant
     · Workflow State Machine (stage gates + human sign-off tracking)
     · Audit Trail Compiler (per-transaction folder, PR→PO lineage)
```

The public-bidding track and the PR/RFQ/shopping track are really **two entry points into the same four shared modules** — you don't need to build a separate system for each; the workflow state machine just needs two different stage sequences (one long for public bidding, one short for shopping) feeding the same document checker, drafting assistant, and audit compiler.

### Registry / numbering detail worth building early
A surprising amount of audit pain in PH government procurement comes from inconsistent or gapped document numbering (PR numbers, RFQ numbers, Resolution numbers out of sequence or duplicated across units). A simple **centralized numbering service** — one API that issues the next PR#/RFQ#/Resolution#/PO# per type per year, atomically, logged — is low effort and prevents a recurring, very real audit finding. Build this before the drafting assistant, since every generated document depends on it.

---

## RAG Layer (Retrieval-Augmented Generation)

Right now the fine-tuned model reasons only over the fields it's given. RAG adds a knowledge base the model can pull from at inference time — this matters a lot here because procurement rules change (GPPB issues new resolutions, COA circulars update thresholds, RA 9184 itself was modernized by RA 12009) faster than you'll want to re-fine-tune.

### What to index
- **RA 9184, its 2016 IRR, and RA 12009** (the modernization law) — full text, chunked by section
- **GPPB Resolutions and Circulars** — these amend/clarify procurement rules regularly and are the most likely source of "the rules changed since the model was trained"
- **COA Circulars** relevant to procurement documentation and audit findings
- **DICT's own procurement manual/BAC internal guidelines**, if RO2 has one
- **Historical BAC Resolutions and decisions** (anonymized) — lets the model retrieve "how was a similar case handled before" as precedent, which is genuinely useful for consistency across BAC decisions
- **PhilGEPS registration requirement references** and standard eligibility document templates

### Where RAG plugs into the architecture
```
User/system query ("is a notarized SEC certificate required for this bid?")
        │
        ▼
 [Retriever] — embedding search over indexed RA 9184/IRR/GPPB/COA corpus
        │
        ▼
 [Retrieved passages] ──┐
                         ├──▶ [Fine-tuned Qwen2.5-3B] ──▶ Answer + citation
 [Extracted fields] ─────┘       (cites section numbers,
                                   not just free-text answers)
```

- Use this for **both** the document checker and the drafting assistant: when the model flags a document as missing/invalid, it should cite *which rule* it's applying (e.g., "required per 2016 IRR Sec. 23.1") rather than asserting it from fine-tuned weights alone. This is important for a government context — staff and auditors need to trace *why* the system flagged something back to an actual rule, not just trust the model.
- **Practical setup**: a local vector DB (Qdrant or Chroma — both run fine on your VPS, no GPU needed for retrieval itself) + a small embedding model (e.g., `bge-small-en` or a multilingual embedding model given Taglish text) for indexing and search.
- **Keep retrieval and generation separate in your evaluation**: test retrieval accuracy (does it pull the right section?) separately from generation accuracy (does the model use it correctly?) — conflating the two makes debugging much harder.

---

## Self-Improving Loop (Human-Gated Continuous Fine-Tuning)

"Self-improving" needs a specific, bounded meaning here — in a government compliance system, you want the model to get better over time **without ever auto-deploying an untested version into production**. The loop below improves the model continuously but keeps a human gate before any new version goes live, for the same reason BAC Resolutions stay human-signed: an unreviewed model update that silently changes how documents get flagged is an audit and compliance risk, not just a technical one.

### The loop
```
 [1] Production model in use
        │
        ▼
 [2] Capture feedback
     - Every time BAC staff override/correct an AI verdict
       (e.g., AI said "complete", human found it actually missing
       a document), log the correction as a new labeled example
     - Every RAG citation that staff flag as wrong/outdated gets logged too
        │
        ▼
 [3] Periodic retraining batch (e.g., monthly or every N corrections)
     - Accumulated corrections become new fine-tuning data
     - Combine with original training set (don't fully replace it —
       prevents the model forgetting earlier-learned cases)
        │
        ▼
 [4] Retrain candidate model (cloud GPU, same QLoRA pipeline as before)
        │
        ▼
 [5] Evaluate candidate against a FIXED, held-out benchmark set
     - Must meet or beat the current production model's accuracy
     - Run on real historical cases with known-correct verdicts
        │
        ▼
 [6] HUMAN REVIEW GATE ◀── required, not optional
     - A person (not the AI) reviews the evaluation report and approves
       promoting the candidate to production
        │
        ▼
 [7] Versioned deployment
     - Old model kept available for rollback
     - Every verdict logged with which model version produced it,
       so you can always trace a past decision back to the exact
       model that made it
```

### Why the human gate is non-negotiable here
This isn't just caution for its own sake — if a procurement decision gets challenged or audited months later, you need to be able to show *which version* of the model produced a given flag and *that a human approved that version's deployment*. A fully autonomous self-improvement loop (model retrains and redeploys itself with no review) would make that traceability impossible to defend, and is also simply more likely to silently drift away from correct RA 9184 interpretation without anyone noticing until an audit catches it.

### Practical additions this requires
- **Correction logging UI**: whenever BAC staff override an AI verdict, a lightweight form capturing "what the AI said / what's actually correct / why" — this is your training data pipeline, not an afterthought
- **Model version registry**: simple table — version, training data snapshot, eval score, approver, deployment date
- **Eval benchmark set**: a fixed, never-touched set of real historical cases with confirmed-correct verdicts, used to score every candidate the same way every time, so you can compare versions apples-to-apples over time

---

## Updated Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                 Procurement Orchestration Layer                │
└──────────────────────────────────────────────────────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
  ┌──────────┐  ┌──────────────┐ ┌───────────┐ ┌──────────────┐
  │ Document │  │ Doc Drafting │ │ Timeline/ │ │ Audit Trail  │
  │ Checker  │  │ Assistant    │ │ Deadline  │ │ Compiler     │
  │ (fine-   │  │ (PR/RFQ/BAC/ │ │ Tracker   │ │              │
  │ tuned +  │  │ POs/Contract)│ │           │ │              │
  │ RAG)     │  │ (+ RAG)      │ │           │ │              │
  └────┬─────┘  └──────┬───────┘ └───────────┘ └──────────────┘
       │               │
       ▼               ▼
  ┌─────────────────────────────┐
  │   RAG Knowledge Base          │   ┌───────────────────────┐
  │   (RA 9184/IRR, RA 12009,     │◀──│ Correction Logging &   │
  │   GPPB, COA circulars,        │   │ Retraining Pipeline    │
  │   past BAC decisions)         │   │ (human-gated versioning)│
  └─────────────────────────────┘   └───────────────────────┘
```

---

## Practical Build Order

Given you already have the document-checker fine-tune planned, build in this order:

1. **Centralized numbering service** — PR#/RFQ#/Resolution#/PO# issuance, atomic and logged; trivial to build, prevents the most common audit finding, and every other module depends on it
2. **Document checker** (already planned) — core completeness/validity engine, reused across bid docs, RFQ quotations, and BAC-stage attachments
3. **Workflow state machine** — tracks stage, deadlines, required human approvers for both the public-bidding track and the shorter PR→RFQ→Resolution→PO track; pure business logic + DB, no model needed
4. **Document drafting assistant** — PR, RFQ, BAC Resolution, PO, NOA/NTP/Contract drafts, reusing your fine-tuned model or a general instruct model with RA 9184 templates in context
5. **Audit trail compiler** — Stage 9, pure logic, highest ROI-to-effort ratio, build this early since it's cheap and immediately useful for COA readiness
6. **RAG knowledge base** — index RA 9184/IRR/RA 12009/GPPB/COA corpus; plug into both the checker and drafting assistant for citation-backed outputs
7. **Monitoring dashboards** — Stage 7, ties into your existing project-tracker idea
8. **Correction logging + retraining pipeline** — build this last, after you have enough production usage generating real corrections to retrain from; premature without Stage 1-7 live

This lets you ship value incrementally instead of needing the "whole procurement AI" built before anything is usable.
