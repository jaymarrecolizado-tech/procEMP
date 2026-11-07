# ProcEMP — Plan (AI-assisted procurement assistant)

> **Role of AI in ProcEMP:** suggestions, guidance, and automation to *aid* BAC/TWG/HOPE/Inspection/Accounting decision-making and reduce document/process errors.  
> **Not in scope for AI:** making or silently finalizing statutory procurement decisions. Named human bodies decide and sign; hard gates, segregation of duties, evidence, citations, and audit trails remain mandatory (see `README.md`, `procurement-review/01-revised-workflow-RA12009.md`).

## 1. Current status (prototype)

All 10 build-order steps are implemented in `procurement-core/` (Python stdlib-only). **160 tests passing** (`cd procurement-core && python -m unittest discover -s tests -v`). See `README.md` and `procurement-review/01-revised-workflow-RA12009.md` §9.

| Area | Status | Key modules/files |
|---|---|---|
| Rules/regime, ledger, numbering, state machine, human gates | Done (prototype) | `core.py`, `schema.sql`, `seed_rules.json` |
| Deterministic checker | Done | `checker.py` |
| OCR/extraction adapter (Qwen plug-in point) | Done (stub backend) | `extraction.py`, `seed_extraction_schema.json` |
| As-of RAG + citation verification | Done (seed corpus) | `rag.py`, `seed_corpus.json` |
| GPPB form registry + deterministic drafting | Done (draft/release controls) | `forms.py`, `seed_forms.json` |
| Contract monitoring + dashboard | Done | `monitoring.py` |
| COA packaging, WORM sealing, verification | Done | `audit.py`, `coa_server.py` |
| Human-gated correction/retraining pipeline (governance) | Done (no real training job) | `learning.py` |

**Prototype stubs to replace:** real Qwen2.5-3B fine-tune served on loopback; real embedding model (`bge-m3`/`multilingual-e5`); full verified legal/form corpus; official form bodies registered by a named human; PostgreSQL/WORM-backed store; real benchmark cases + correction UI + QLoRA training job; authenticated deployment hardening.

### Model status (extraction/RAG) — as of 2026-11-07
- **No trained/fine-tuned model or model weights are in this repo** (no `.gguf`/`.safetensors`/`.bin`/`.pt` checked in).
- **Extraction (implemented, stubbed):** `procurement-core/extraction.py` provides `StubBackend` (deterministic tests/demo), `valid_stub_response()`, and `LocalHTTPBackend` (loopback-only). `LocalHTTPBackend` expects `POST` JSON `{model_version, schema, pages, options}` and a schema-valid extraction JSON response (see `seed_extraction_schema.json`). ProcEMP validates output and **fail-closes to `ABSTAIN`** on schema/confidence/type/hallucination violations — the model never makes procurement decisions.
- **Local LLM integration scaffolding (prototype, implemented + tested):** `procurement-core/model_adapter_server.py` can front a loopback LLM runtime (Ollama / OpenAI-compatible / llama.cpp) and adapt responses to the ProcEMP contract (advisory; base/non-fine-tuned models may have low schema-valid rates). `procurement-core/model_plumbing_check.py` validates endpoint contract/latency against approved, non-confidential samples. `procurement-core/training_adapter.py` builds JSONL/SFT records and documents the backend contract. `procurement-core/dataset.py` + `evaluation.py` + `training_job.py` add the dataset plan, frozen-slice eval, and human-gated orchestration; **no fine-tuned weights exist yet** (GPU training is an external approved-environment step).
- **Embeddings/RAG:** `rag.py` uses stubs in the prototype (replace with `bge-m3`/`multilingual-e5` in a controlled, local environment). No embedding model files bundled.
- **Quick plumbing check:**
  ```bash
  cd procurement-core
  python model_plumbing_check.py stub --port 8080          # contract-compatible stub
  python model_plumbing_check.py check --url http://127.0.0.1:8080/extract
  ```
  Replace the URL with a real loopback server/adapter when available. Use only approved, non-confidential samples and follow RA 10173/NPC/DPIA/data-governance requirements (`procurement-review/05-data-governance-training.md`).

### Implementation cross-check (2026-11-08)
- **Result:** all `[x]` items in this plan are implemented as **prototype/scaffolding** consistent with their descriptions/caveats; all `[ ]` items remain not done and the implementation correctly reflects that (fail-closed blockers, no trained model weights/runtime in repo, no verified/approved seed content or official form hashes registered, no external/DPO/legal sign-offs, no production DB/WORM/auth deployment). No item is falsely marked `[x]`.
- **Evidence:** 138/138 tests passing (`test_core` 105, `test_queue` 18, `test_tracks` 15); key modules import; `readiness_report.py` reports `ready_for_production:false` (advisory) with expected blockers tracked in `procurement-review/07-readiness-worklist.md`.
- **Main remaining dependencies:** external/legal/DPO confirmations + verified official content (Track B), authenticated/rehearsed deployment (Track C/ops), and an approved local runtime + real anonymized samples + human-gated fine-tune/eval (Track A).

## 2. Product goals (assistant-first)

1. **Decision support for BAC members** — clear, evidence-backed suggestions (flags, checklists, comparisons, risk notes) with confidence, rule citations, and `ABSTAIN` when uncertain; never auto-disqualify/auto-award.
2. **Properly formatted procurement documents** — deterministic, registry-driven drafts from approved data (GPPB Standard/Recommended forms as applicable), labeled as AI-assisted drafts until human review/sign-off and required authorizations are in place.
3. **Process automation to minimize human error** — regime/rule checks, numbering, required-document validation, timeline/milestone/LD/VO flags, and COA-ready packaging, all auditable.
4. **Traceability & compliance posture** — every suggestion/verdict tied to `model_version`, `ruleset_version`, `corpus_snapshot_id`, evidence, and human actions; fail closed on ambiguity.

## 3. What we need to do now (prioritized)

### Implemented in this cycle
- [x] **BAC review workflow/queue (Track C, prototype):** `procurement-core/review_queue.py` + schema tables (`review_item`, `review_action`) provide an advisory queue for ABSTAIN/FAIL/flagged verdicts with CLI/JSON/HTML export (`export`), claim/recommend/resolve actions, and override → correction submission (`learning.submit_correction`, PENDING until second-reviewer adjudication). `core.submit_verdict()` now auto-enqueues ABSTAIN/FAIL verdicts (best-effort, advisory). Ledger events are recorded; queue never makes statutory decisions. `procurement-core/review_server.py` adds a loopback-only web UI for read + controlled advisory actions.
- [x] **Extraction/QLoRA scaffolding (Track A, prototype):** `procurement-core/training_adapter.py` defines JSONL SFT/completion record builders (schema-validated target), documents the `LocalHTTPBackend` POST contract, and provides a loopback stub server for integration tests. `procurement-core/model_plumbing_check.py` validates loopback endpoint/schema conformance and latency before fine-tuning (advisory plumbing check only).
- [x] **Data governance draft:** `procurement-review/05-data-governance-training.md` drafted for DPO/legal/BAC Secretariat review (RA 10173/NPC/DPIA, anonymization, locality, adjudicated corrections), now with draft appendices for RBAC/SoD matrix, dataset provenance template, and approved environments (§8–§10).
- [x] **Mode/regime checklist coverage (Track B):** `procurement-core/checklist_builder.py` builds deterministic BAC-approvable templates for RA12009 Competitive Bidding + SVP from verified rules; unsupported modes/fail-closed legacy (RA9184 unverified) — never auto-invented. The checker still requires a named `approved_by` per transaction.
- [x] **Dataset plan + frozen eval scaffolding (Track A):** `procurement-core/dataset.py` (synthetic anonymized-only samples across 5 slices: clean_digital/taglish_mixed/sparse_scan/unreadable/missing_field, SFT JSONL + benchmark exports, provenance `real_training_approved: false`) and `procurement-core/evaluation.py` (frozen-slice eval + confidence calibration buckets, bridges to `learning.evaluate_candidate` per-slice non-regression; human approval stays a separate gate).
- [x] **Training orchestration (Next):** `procurement-core/training_job.py` wires `learning.create_training_batch` → SFT export → `register_candidate` (CANDIDATE only) → weights-hash registration + frozen eval; GPU training itself is an external approved-environment step; approve/canary/promote/rollback remain named-human steps (no auto-deploy).
- [x] **Review UI hardening (Track C, phase 1):** `review_server.py` now supports shared-secret loopback auth (`--auth-token`/`REVIEW_AUTH_TOKEN`, `X-Auth-Token` or `?token=`/form-field, constant-time compare), removes the `/export` arbitrary-file-write endpoint (CLI-only now), adds provenance + evidence-span table to the item viewer, and enforces the RBAC matrix in `review_queue.ROLE_PERMISSIONS` (resolve = BAC members/chairs; override = staff/secretariat/TWG; adjudicate = second-reviewer/chair/secretariat + SoD). Reverse-proxy/agency-SSO stays an operator step (`procurement-review/06-deployment-runbook.md`).
- [x] **Backup/restore drills + retention (Next):** `procurement-core/ops_backup.py` (consistent SQLite backup + manifest, verify, restore-drill on a copy, advisory retention inventory). PostgreSQL/WORM migration remains an external target.
- [x] **Tests + runbook:** added `procurement-core/tests/test_queue.py` + `test_tracks.py` (33 new tests: queue/RBAC, readiness, adapter/plumbing, checklist builder, dataset, eval, training job, backups, server auth); `procurement-review/06-deployment-runbook.md` documents loopback services, queue ops, eval, training, backups, and migration.
- [x] **Authenticated COA access (Next):** `procurement-core/coa_server.py` now supports shared-secret loopback auth (`--auth-token`/`COA_AUTH_TOKEN`, `X-Auth-Token` or `?token=`/`?auth_token=`, constant-time compare; `/health` stays open for probes). The offline CLI is unaffected (local file ops). `handle()` keeps its pure-testable signature (defaults to no-auth for backward compatibility).
- [x] **TF-IDF embedding backend (Next, intermediate prototype):** `procurement-core/rag.py` adds `TfidfEmbedding` (deterministic, fitted on corpus texts; `fit()` + L2-normalized vectors) as a pluggable `AsOfRAG` backend between the hashing `StubEmbedding` and a future `bge-m3`/`multilingual-e5`. Real neural embeddings remain an external/controlled-environment step.
- [x] **Worklist generator (Track B follow-through):** `readiness_report.py --format worklist` now renders `07-readiness-worklist.md` directly from live audit data (transition/rules table/corpus/forms/external/engineering sections); `07-readiness-worklist.md` was regenerated so the worklist tracks the seeds.
- [x] **Loopback runtime probe (Track A scaffolding):** `model_plumbing_check.py probe` detects Ollama/llama.cpp/vLLM/ProcEMP-adapter runtimes on loopback with GET-only health/tags discovery (no procurement/sample data sent, non-loopback refused). Currently reports 0 reachable in this dev environment — the real runtime step remains external, but detection is now one command.
- [x] **Richer review viewers + role affordances (Track C):** the item page now shows rule-citation passage excerpts with live `AsOfRAG.verify` status, ingested-document metadata table (metadata only — content never stored), and a ledger timeline; action panels list allowed roles (`ROLE_PERMISSIONS`), role inputs use a datalist of known roles, and the adjudicate panel exposes the actor-role field. Native TLS is added separately below; an authenticated proxy remains an operator step.
- [x] **Native TLS + mutual TLS + defensive headers (Track C, code-side):** new `procurement-core/ops_tls.py` adds operator-supplied TLS to both loopback services (`--tls-cert`/`--tls-key`, optional `--tls-client-ca` for mutual TLS; env fallbacks `REVIEW_*`/`COA_*`; TLS 1.2+). Half-configured TLS fails closed instead of silently serving plaintext. Both servers now send no-sniff, frame-deny, referrer, COOP/CORP, permissions-policy and CSP headers, plus HSTS when TLS is active. Certificates/keys remain operator-supplied (PKI/agency CA); transport protection does not replace human gates. Tests cover header emission, TLS argument/env handling and fail-closed configuration.
- [x] **Docs sync (ops):** `06-deployment-runbook.md` documents TLS/mTLS flags, env vars, certificate handling and what remains an operator step; `README.md`/`plan.md` test counts and module map updated.
- [x] **Read-only deployment smoke check (ops follow-through):** `procurement-core/ops_smoke.py` rehearses the runbook automatically — ledger chain, corpus passage hashes, seed/registry readiness (advisory) and optional loopback `/health` polling for the review UI/COA services. No writes, no decisions; non-loopback URLs are refused; exit 0 only when error-severity checks pass. Live target-environment rehearsal remains an operator step.

### Now (next 1–2 execution cycles)
- [ ] **A. Extraction assistant (Qwen2.5-3B path) — code scaffolding done; external/real-data steps remain:** synthetic dataset plan (`dataset.py`), frozen-slice eval + calibration (`evaluation.py`), and training orchestration (`training_job.py`) are implemented and tested. **Still needed (external/approved environment only):** real anonymized/approved base samples (fields, confidence/evidence, `UNREADABLE` cases, Taglish/scans) with DPO/BAC sign-off; run a real loopback model runtime and validate with `model_plumbing_check.py`/`model_adapter_server.py` (no runtime detected in the current dev environment); QLoRA SFT train against `seed_extraction_schema.json`; confidence/abstention calibration on real samples; frozen-slice eval + per-slice non-regression *on the candidate* before any human promotion. **No fine-tuned weights in repo; no auto-deploy.**
- [ ] **B. Rules/forms/corpus quality — checklist builder + readiness + worklist done; verified-content steps remain:** `checklist_builder.py` covers RA12009 Competitive Bidding + SVP (other modes fail closed). `readiness_report.py` reports unverified/placeholder rules, unverified corpus chunks, missing official form bodies/hashes, and fail-closed blockers (`--format json|md`, optional `--db`). `procurement-review/07-readiness-worklist.md` tracks owners/actions for those blockers. **Still needed (external, named humans only):** resolve transition policy; confirm/populate legacy RA 9184 placeholders (Annex H/checklist/posting) with authority citations; replace unverified legacy corpus chunk(s) and expand the verified GPPB/COA/issuance corpus (new snapshot, never silently repin); register official form-body hashes for all required forms (`forms.register_official_form`) and confirm GPPB online-form authorization where applicable. Do not invent values.
- [ ] **C. BAC review workflow/UI — phase-1 hardening + viewers + native TLS done; proxy/rehearsal remain:** `review_server.py` has shared-secret loopback auth, native TLS/mTLS (`ops_tls.py`), defensive headers, RBAC enforcement, provenance/evidence-span viewer, `/export` CLI-only, citation passage+verification, document-metadata and ledger viewers, and role-aware affordances; `06-deployment-runbook.md` documents loopback services/ops. **Still needed (operator/external):** an authenticated reverse proxy in front for any LAN/VPS exposure (native TLS covers transport, not agency SSO), agency PKI certificate issuance, and runbook rehearsal on the target environment. All actions remain advisory/audited with statutory human sign-offs/gates.
- [ ] **Confirm external/legal dependencies** (owner: BAC Secretariat / legal / DPO): GPPB transition guidance + legacy Annex H thresholds; GPPB authorization for online form accomplishment (as applicable); PhilGEPS posting/API approach; e-sign (PNPKI/RA 8792) vs wet-ink procedure; DPIA/RA 10173/NPC obligations; agency decision-support instrument.
- [ ] **Operationalize data governance — appendices drafted; sign-off remains:** `05-data-governance-training.md` §8–§10 now include draft RBAC/SoD matrix, provenance template, and approved environments for inference/training/corrections. **Still needed:** DPO/legal sign-off, DPIA/NPC completion, staffing of second reviewers, and retention/backup drills on the approved environment.

### Next
- [ ] Replace `StubBackend`/embedding stubs in a controlled environment (`TfidfEmbedding` is the intermediate prototype; `bge-m3`/`multilingual-e5` remain the production target); run extraction + RAG on real sample documents (non-production) with human verification.
- [ ] Operate the real QLoRA training job in the approved GPU environment (orchestration wired to `learning.py` via `training_job.py`: batch creation, candidate registration, frozen benchmark evaluation) with human approval, canary, and rollback — **no auto-deploy**; the GPU job itself remains external.
- [ ] Register official Res. 03-2025 form bodies (human-hashed) and validate generated drafts against required authorizations.
- [ ] Expand corpus to full verified GPPB/COA/issuance set; resolve legacy-placeholder passages.
- [ ] Move persistence toward production targets (PostgreSQL, WORM-backed append-only store); authenticated COA access path is implemented in `coa_server.py` (shared-secret loopback auth, proxy still recommended); backup/restore drills + retention schedule are implemented in `ops_backup.py` and need rehearsal on the target environment.

### Later
- [ ] Full end-to-end pilot on a limited procurement mode with BAC Secretariat/legal oversight.
- [ ] Mode-specific workflows beyond SVP/competitive baseline stubs, only where rules/forms are verified.
- [ ] Security hardening, monitoring, and operational runbooks for workstation → VPS deployment.

## 4. Guardrails (must remain true)

- AI outputs are **advisory**; statutory determinations require named human sign-offs recorded in the ledger.
- **Fail closed**: missing/ambiguous rule, low confidence, schema/citation mismatch, or missing signature → `ABSTAIN`/blocked transition, never a silent guess.
- **Bid/confidential data stays local** (loopback-only inference; follow DPIA/NPC requirements).
- **Rules and forms in versioned data**, resolved as-of transaction context; corrections improve models only through the adjudicated, human-gated pipeline.
- **No auto-deploy** of model/corpus changes; maintain provenance and rollback.

## 5. References

- `README.md` — overview, quick start, before-production notes
- `procurement-review/01-revised-workflow-RA12009.md` — revised workflow and build order
- `procurement-review/02-risk-gap-register.md` — risks/gaps and controls
- `procurement-review/03-tech-spec-rules-evidence-gate.md` — technical spec and acceptance criteria
- `procurement-review/04-compliance-checklist.md` — go-live/compliance gate
- `procurement-review/05-data-governance-training.md` — training/correction governance (draft + appendices)
- `procurement-review/06-deployment-runbook.md` — loopback services, queue ops, training, backups
- `procurement-review/07-readiness-worklist.md` — Track B fail-closed blockers/worklist (regenerate with `readiness_report.py`)
- `procurement-core/demo.py`, `procurement-core/tests/` — runnable scenario and 160 tests
