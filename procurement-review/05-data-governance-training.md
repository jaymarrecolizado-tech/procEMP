# Data Governance — Training, Corrections, and Confidential Procurement Data

**Status:** Draft for DPO / legal / BAC Secretariat review (not a legal opinion)  
**Related plan:** `plan.md` §3 “Set training/data governance”

This document defines minimum governance expectations for ProcEMP’s AI-assisted extraction/correction/retraining workflow. It must be validated against **RA 10173 (Data Privacy Act)**, NPC issuances, agency policy, and applicable procurement/confidentiality rules before production use.

## 1. Principles

- **Decision support only.** AI outputs are advisory; statutory procurement decisions remain with authorized human bodies.
- **Confidentiality/locality.** Bid and confidential procurement data must not be sent to external/cloud AI APIs. Inference and retrieval run locally/loopback (`extraction.LocalHTTPBackend` refuses non-loopback endpoints).
- **Minimum necessary.** Collect, retain, and use only data needed for operations, audit, and approved model improvement.
- **Traceability.** Every verdict/correction/model/corpus change must be attributable (named actor, timestamp, version IDs, hashes/events).
- **Human-gated improvement.** Corrections become training-eligible only after second-reviewer adjudication; model/corpus promotion requires human approval and rollback capability (`learning.py`).

## 2. Data categories

| Category | Examples | Handling notes |
|---|---|---|
| Operational procurement records | PR/RFQ/PO/contract metadata, stages, sign-offs, ledger events | Governed by COA/records-retention and access controls; audit evidence. |
| Bid/confidential content | Submitted bids, eligibility/technical/financial documents, sealed envelopes | Highest sensitivity. Enforce custody/opening gates; no off-box processing; strict RBAC; log access. |
| Extraction outputs | Typed fields, confidence, evidence spans, verdicts | Advisory; store provenance (`verdict_id`, `model_version`, `ruleset_version`, `corpus_snapshot_id`, `prompt_hash`). |
| Corrections/feedback | AI value vs corrected value, reason, submitter/adjudicator | Contains potentially sensitive context. Use second-reviewer adjudication; keep audit trail; avoid unnecessary copying of source documents. |
| Training datasets | Base training set, accepted correction batches, benchmarks | Must follow anonymization/minimization rules; no use of sealed/confidential bid data without explicit legal/DPO basis and controls. |
| Model/corpus artifacts | Weights, configs, QLoRA adapters, snapshots, benchmarks | Versioned, access-controlled, integrity-checked; deployment only through human-gated pipeline. |

## 3. Allowed use and approvals

- **Production operations:** only approved rulesets/corpus snapshots and approved model versions may be used for advisory outputs.
- **Corrections:** staff may submit overrides; a **different** second reviewer must adjudicate before data is training-eligible. Submitter cannot self-adjudicate.
- **Training:** a training batch requires adjudicated accepted corrections and replay of the base training set (as implemented in `learning.py`). Training jobs must run in an approved environment with no external data exfiltration.
- **Benchmark cases:** frozen and immutable after registration; include source references and named human who froze them. Real historical cases require confirmed-correct verdicts and anonymization review.
- **Data sharing:** do not share raw bid/confidential documents, training datasets, or model artifacts outside authorized systems/personnel without DPO/legal approval.

## 4. Anonymization and minimization (for training/benchmarks)

Before using historical records for training or benchmark slices:
- Remove or pseudonymize direct identifiers (names, addresses, contact details, TIN-like identifiers, signatures/IDs) unless legally required and approved.
- Minimize retained text/images to fields needed for the extraction task.
- Keep a mapping/re-identification process only if necessary, under strict access control and documented lawful basis.
- Record the anonymization method, reviewer, date, and scope in dataset provenance.
- Treat anonymization as a risk-reduction control, not a guarantee; obtain DPO review.

## 5. Access control and audit

- Apply least privilege and segregation of duties (drafter ≠ approver; correction submitter ≠ adjudicator; canary deployer ≠ production promoter where applicable).
- Log access and changes in the append-only ledger/audit mechanisms where applicable.
- Restrict model training/export functions to authorized ML/admin roles.
- Review queue actions are advisory workflow records; statutory determinations require governed sign-offs.

## 6. DPIA / NPC / records obligations (confirm before production)

The following require DPO/legal confirmation and are **not** satisfied by this prototype:
- DPIA scope and findings for processing procurement/bid data and AI-assisted review.
- NPC registration/notifications/compliance steps applicable to the agency/system.
- Lawful basis, privacy notice, data subject rights handling, and breach response procedures.
- Records-retention schedule and COA/audit archive requirements.
- Any GPPB/agency authority needed for electronic/online handling of forms or records.

## 7. Operational checklist

- [ ] DPO/legal review completed; DPIA/NPC obligations documented.
- [ ] Approved environments identified for inference, correction handling, and training (all local/controlled; no external AI APIs).
- [ ] RBAC and segregation-of-duties matrix approved.
- [ ] Dataset provenance template adopted (source, scope, anonymization, approver, hash/snapshot).
- [ ] Correction adjudication workflow staffed (second reviewers identified).
- [ ] Benchmark freeze/approval process defined.
- [ ] Model/corpus promotion and rollback runbook approved.
- [ ] Backup/restore and retention controls tested.

## 8. Appendix — RBAC / segregation-of-duties matrix (draft for DPO/legal approval)

Enforced in code by `review_queue.ROLE_PERMISSIONS` + `learning.py` SoD checks. Advisory workflow only; statutory determinations need governed sign-offs.

| Action | Allowed roles | Notes |
|---|---|---|
| CLAIM / ACK / NOTE | BAC_MEMBER, BAC_CHAIR, BAC_SECRETARIAT, BAC_STAFF, TWG, SECOND_REVIEWER | Any named reviewer; decisions still need sign-offs |
| RECOMMEND_CONFIRM / REJECT / REVIEW | BAC_MEMBER, BAC_CHAIR, BAC_SECRETARIAT, TWG, HOPE | Advisory recommendation, never a determination |
| RESOLVE (advisory item) | BAC_MEMBER, BAC_CHAIR, BAC_SECRETARIAT | Closes the queue item, not the procurement |
| REQUEST_CORRECTION (override) | BAC_STAFF, BAC_SECRETARIAT, TWG | PENDING until second-reviewer adjudication |
| Adjudicate correction | SECOND_REVIEWER, BAC_CHAIR, BAC_SECRETARIAT | Submitter can never self-adjudicate |
| Approve model | Named human approver (separate step) | After passing frozen eval; no auto-deploy |
| Canary → PRODUCTION promote | Named human (different from deployer) | Second sign-off required |

## 9. Appendix — Dataset provenance template (draft)

Record for EVERY training/benchmark dataset version:

| Field | Example |
|---|---|
| Dataset ID / version | base-synthetic-v1 |
| Source | synthetic generator (no confidential data) / approved historical sample set |
| Scope (slices) | clean_digital, taglish_mixed, sparse_scan, unreadable, missing_field |
| Anonymization method + reviewer | N/A synthetic / method + name + date (for real samples) |
| Approver (named human) | name + role |
| Hashes | dataset sha256, benchmark_snapshot_hash (DB), schema_version |
| Lawful basis / DPO sign-off ref | DPIA ref + date (required for real samples) |
| Approved environment | inference / correction / training (all local) |

Generated synthetic sets (`dataset.py`) are marked `real_training_approved: false`.

## 10. Appendix — Approved environments (draft)

| Purpose | Approved environment | Barred |
|---|---|---|
| Inference (extraction) | Loopback server on agency workstation/VPS; `LocalHTTPBackend` refuses non-loopback | External/cloud AI APIs |
| Correction handling | Same DB + review UI (loopback, token auth) | Sharing raw bid docs outside authorized personnel |
| Training (QLoRA) | Approved local GPU environment; SFT JSONL validated in-repo | Exfiltration of sealed/confidential data |
| COA verification | Read-only loopback endpoint / offline CLI | Write-capable COA access |