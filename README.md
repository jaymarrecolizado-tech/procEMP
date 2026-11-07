# ProcEMP — AI-assisted procurement workflow (DICT RO2 prototype)

A working prototype of an AI-assisted government procurement system, built against
**RA 12009 (New Government Procurement Act)** and its IRR, with dual-regime support
for legacy **RA 9184 / 2016 IRR** transactions during the transitory period.

> **Status:** all 10 build-order steps implemented · **138 tests passing** · stdlib-only ·
> **prototype, not a deployable government system**. No model weights, embedding models,
> official form bodies or production database are bundled. See [Before production](#before-production).

The governing rule throughout: **AI drafts, flags and recommends; named human bodies
(BAC, TWG, HOPE, Inspection Committee) decide and sign.** No stage lets a model make
or silently finalize a statutory decision.

---

## Quick start

Requires Python 3.10+. No third-party packages.

```bash
cd procurement-core

# Run the full acceptance suite (138 tests)
python -m unittest discover -s tests -v

# Run the end-to-end scenario (14 steps, prints each control firing)
python demo.py
```

`demo.py` walks one transaction from creation through sealed-bid custody, evaluation,
citation-backed extraction, deterministic drafting, contract monitoring, the human-gated
retraining loop, a WORM-sealed COA folder, and a local→VPS migration — all against an
in-memory SQLite database. It also regenerates `sample-dashboard.html` in this folder
(gitignored; open it in a browser to see the monitoring dashboard).

No network calls are made by the tests or demo. No external AI/embedding API is used.

---

## Repository layout

```
procurement-review/            Design and compliance documents
  00-source-workflow.md        The original RA 9184-based design (for comparison)
  01-revised-workflow-RA12009.md  Current design, dual-regime, build order, §9.1 remaining work
  02-risk-gap-register.md      27 findings with severity, evidence, owner, fix
  03-tech-spec-rules-evidence-gate.md  Deep spec + acceptance criteria A1–A45
  04-compliance-checklist.md   COA / NPC / GPPB go-live checklist

procurement-core/              Runnable prototype (Python stdlib only)
  core.py                      Rules registry, regime resolver, append-only hash-chained
                               ledger, numbering, state machine, human gates, migration
  checker.py                   Deterministic pass/fail rules engine (no LLM in decisions)
  extraction.py                OCR/extraction adapter — the Qwen2.5-3B plug-in point
  rag.py                       As-of retrieval + citation verification
  forms.py                     GPPB Res. 03-2025 form registry + deterministic drafting
  monitoring.py                Contract milestones, delay/LD/VO flags, HTML dashboard
  audit.py                     COA folder compile, WORM sealing, read-only verification
  coa_server.py                GET-only verification endpoint + offline CLI
  learning.py                  Correction logging + human-gated retraining loop
  review_queue.py              Advisory BAC review queue (RBAC, HTML/JSON export)
  review_server.py             Loopback web UI (token auth, evidence viewer)
  checklist_builder.py         Mode/regime checklist templates (fail closed)
  dataset.py                   Synthetic anonymized dataset plan + benchmark
  evaluation.py                Frozen-slice eval + confidence calibration
  training_job.py              Human-gated QLoRA orchestration (no auto-deploy)
  training_adapter.py          SFT record builders + loopback stub server
  model_plumbing_check.py      Loopback endpoint/schema/latency validation
  model_adapter_server.py      Schema-constrained local-LLM adapter
  readiness_report.py          Seeds/registry readiness + fail-closed blockers
  ops_backup.py                Backup / verify / restore-drill / retention
  demo.py                      End-to-end scenario
  schema.sql                   SQLite schema (append-only triggers, immutability)
  seed_rules.json              Versioned rules for both legal regimes
  seed_forms.json              36 GPPB forms, Standard vs Recommended
  seed_corpus.json             13 verified official legal passages (verbatim excerpts)
  seed_extraction_schema.json  Typed extraction output contract
  tests/                       138 tests (core + queue + tracks)
```

---

## Module map

| Step | Module | What it guarantees |
|---|---|---|
| 1 | `core.py` + `seed_rules.json` | Rules live in versioned data; each transaction pins its regime, ruleset and corpus snapshot immutably |
| 2 | `core.py` numbering | Atomic, gap-free PR/RFQ/Resolution/PO numbers with void-with-reason |
| 3 | `core.py` gates | Bid contents cannot be read before the recorded public opening; stage transitions need verifiable human sign-offs |
| 4 | `checker.py` | Deterministic pass/fail; an unreadable scan abstains rather than disqualifying a bidder |
| 5 | `forms.py` + `seed_forms.json` | Field-fill drafting only, no generated legal prose; release blocked until official form bodies are hash-registered |
| 6 | `extraction.py` | Strict typed schema, loopback-only inference, abstain on low confidence or hallucinated fields |
| 7 | `rag.py` + `seed_corpus.json` | As-of retrieval pinned to the transaction; a citation to a nonexistent section cannot be fuzzy-matched |
| 8 | `audit.py` + `coa_server.py` | Immutable sealed folders, hash-chain attestation, read-only COA verification |
| 9 | `monitoring.py` | Human-verified milestones; liquidated damages are advisory estimates, never determinations |
| 10 | `learning.py` | Second-reviewer adjudication, frozen benchmark, per-slice non-regression, human approval, canary, rollback |

---

## Legal baseline — read this before changing rules

- **RA 12009 took effect 13 August 2024.** Section 115 of its IRR **repeals RA 9184 and
  Commonwealth Act No. 138**. The original source document in this repo is therefore
  legally stale; `01-revised-workflow-RA12009.md` supersedes it.
- **Dual regime is required.** IRR Sec. 113 gives a three-year transitory period from GPPB
  approval of the Standard Forms (Res. 03-2025, 14 May 2025 → ~May 2028). Until GPPB issues
  the promised guidance on which law applies during the transition, `suggest_regime()`
  returns `AMBIGUOUS` and a legal officer must choose the regime explicitly per transaction,
  with a recorded `legal_basis_note`.
- **Not every GPPB form is a "Standard Form."** Res. 03-2025 classifies Strategic Planning
  Forms, PBD and PBD Related Forms as mandatory Standard Forms. RFQ, Abstract of Bids and
  **Notice of Award are Recommended Forms**, explicitly *not* Standard Forms. The registry
  records 14 Standard / 22 Recommended.
- SVP under IRR Sec. 34: ₱2,000,000 threshold (GPPB-revisable), ≤₱200,000 no posting,
  ≥3 suppliers invited but **one quotation suffices**; award criteria include LCRB, MEARB,
  MARB and HRRB — not automatically the lowest quote.

Values in `seed_*.json` marked `verified: false` are placeholders. The system **fails closed**
when reading them rather than guessing.

Sources: [IRR of RA 12009](https://www.gppb.gov.ph/wp-content/uploads/2025/02/IRR-of-RA-No.-12009.pdf)
and [GPPB Resolution No. 03-2025](https://www.gppb.gov.ph/wp-content/uploads/2025/07/GPPB-Resolution-No.-03-2025.pdf).

---

## COA verification (offline CLI)

The HTTP endpoint is GET-only, read-only and loopback-bound. The CLI works on any
database file without starting a server:

```bash
cd procurement-core
python coa_server.py verify-chain   --db procurement.db
python coa_server.py verify-folder  --db procurement.db --seal-id <seal-id>
python coa_server.py verify-corpus  --db procurement.db
python coa_server.py list-seals     --db procurement.db
python coa_server.py export         --db procurement.db --seal-id <id> --out ./coa-package
python coa_server.py seal           --db procurement.db --tx-id <id> --sealed-by <name>
python coa_server.py serve          --db procurement.db --port 8090
```

Routes: `/health`, `/verify-chain`, `/verify-folder`, `/folder`, `/integrity`, `/list-seals`.
`export` writes `package.json` + `MANIFEST.sha256` and refuses to produce a package if
verification fails. For LAN access, put an authenticated reverse proxy in front — the
server refuses non-loopback binds.

---

## Design principles

1. **Human authority.** Every statutory checkpoint is a hard gate with verifiable identity
   and segregation of duties (drafter ≠ approver).
2. **Fail closed.** Missing rule, low confidence, unverifiable citation, absent signature →
   `ABSTAIN` or a blocked transition. Never a silent best guess.
3. **Rules in data, not weights.** Fine-tuning teaches task behaviour only. Thresholds,
   timelines, modes and forms are versioned configuration resolved *as of* the transaction
   date, never "latest".
4. **Sealed-bid boundary.** Nothing reads bid contents until `BID_OPENING_RECORDED`;
   e-bid passwords are never stored; MEARB/MARB financial envelopes need a second opening.
5. **Evidence before assertion.** Every verdict carries rule citations, retrieved passage
   hashes, OCR evidence spans, model version, corpus snapshot and prompt hash.
6. **Audit from day one.** Append-only hash-chained ledger; sealed folders are WORM;
   every model/corpus change is a human-approved, versioned, rollback-capable event.
7. **Local-first.** All inference and retrieval run in-process or on loopback. No bid data
   leaves the agency machine.

---

## Before production

The build order is complete; this is a **prototype with all control paths implemented and
tested**. Two buckets of work remain (detail: §9.1 of the design doc).

**External / legal confirmations** — all config-level, no code rewrite, but they need your
BAC Secretariat / legal officer:
GPPB transition guidance; exact legacy Annex H thresholds; whether PhilGEPS exposes a
supported posting API; GPPB authorization for accomplishing Standard Forms online;
official form bodies for all 36 forms; e-signature basis (PNPKI / RA 8792) or wet-ink
procedure; DPIA and NPC obligations under RA 10173; an agency instrument defining the
system as decision support; COA/DPO briefings and staff training.

**Prototype stubs to replace:**
SQLite → PostgreSQL; in-process ledger → WORM-backed store; `StubBackend` → the real
Qwen2.5-3B fine-tune served on loopback; `StubEmbedding` → `bge-m3` / `multilingual-e5`;
13 seed passages → the full GPPB/COA/GPPB-circular corpus; placeholder legacy corpus →
verified 2016 IRR text; synthetic benchmark cases → real historical cases with
confirmed-correct verdicts; a real correction UI and QLoRA training job; authenticated
reverse proxy, backup/restore drills and a records-retention schedule.

---

## Notes

- Line endings are normalized to LF (`.gitattributes`). Agent workspace memory under
  `.workbuddy-ai/` is intentionally not committed.
- Everything here is engineering and process control, **not a legal opinion**. Have DICT
  legal counsel and the BAC Secretariat validate against the GPPB issuances current at
  the time of deployment.
- Review document: `procurement-review/02-risk-gap-register.md` lists all 27 open findings
  and their code-level controls; `04-compliance-checklist.md` is the go-live gate.
