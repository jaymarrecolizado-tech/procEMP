# Technical Specification — Rules, Evidence & Gate Layer (REGL)

**Version:** 1.0 · 2026-11-05
**Module choice:** the user requested "a module tech spec" without nominating one. This spec covers **REGL**, the highest-risk foundation: versioned rules, as-of retrieval, the append-only event ledger, and the hard gates (custody boundary + human sign-off). It is build steps 1, 3 and 7 of the revised build order. The document checker and drafting assistant consume it but are specified elsewhere.
**Deployment target:** local workstation → agency VPS, identical containers, no external AI APIs.

---

## 1. Purpose and non-goals

**Purpose:** make it *technically impossible* for the system to (a) apply the wrong legal rule, (b) read sealed bid contents early, (c) advance a transaction without a recorded human sign-off, or (d) produce a decision that cannot be reproduced at audit time.

**Non-goals:** OCR quality, model training, UI design, PhilGEPS integration, document template content.

**Design invariants**

| # | Invariant |
|---|---|
| I1 | A transaction has exactly one immutable `regime`, `ruleset_version`, `corpus_snapshot_id` after creation |
| I2 | No rule value (threshold, deadline, required document, form) exists in application code |
| I3 | No bid content is readable by any service until `BID_OPENING_RECORDED` exists in the ledger |
| I4 | Every stage transition requires all configured `required_signoffs` with verifiable identity |
| I5 | Every verdict is reproducible from `(input_doc_hash, ruleset_version, corpus_snapshot_id, model_version, prompt_hash)` |
| I6 | Ledger rows are append-only; integrity verifiable by an external auditor without application access |

---

## 2. Components

```
┌───────────────────────────────────────────────────────────────────┐
│ API gateway (auth, RBAC, request-id, idempotency key)              │
└──────────┬────────────────────────────────────────────────────────┘
           │
   ┌───────▼────────┐  ┌────────────────┐  ┌──────────────────────┐
   │ Rules Registry │  │ State Machine  │  │ Ledger Writer        │
   │ (versioned)    │◀─│ (gates/timers) │─▶│ (append-only, hash)  │
   └───────┬────────┘  └───────┬────────┘  └──────────┬───────────┘
           │                   │                       │
   ┌───────▼────────┐  ┌───────▼────────┐  ┌──────────▼───────────┐
   │ As-of Retriever│  │ Signature Svc  │  │ Ledger Store + WORM  │
   │ (hybrid, pinned│  │ (PNPKI/RA 8792 │  │ object storage       │
   │  corpus)       │  │  or wet-ink)   │  │ (documents, checksums)│
   └───────┬────────┘  └────────────────┘  └──────────────────────┘
           │
   ┌───────▼────────────────────────────────────────────────────────┐
   │ Consumers: extraction service · rules engine · drafting · audit │
   │ compiler · dashboards · correction pipeline                     │
   └────────────────────────────────────────────────────────────────┘
```

**Storage:** PostgreSQL (rules, transactions, ledger metadata, signature records) · Qdrant/Chroma (vectors) · filesystem/MinIO (documents, WORM option) · Redis (idempotency, timers only — never a system of record).

---

## 3. Data model (abridged DDL)

```sql
CREATE TABLE ruleset (
  id                uuid PRIMARY KEY,
  regime            text NOT NULL CHECK (regime IN ('RA9184_2016IRR','RA12009_IRR')),
  version           text NOT NULL,
  effective_from    date NOT NULL,
  effective_to      date,
  issuance_ref      text NOT NULL,       -- e.g. 'GPPB Res. 03-2025'
  checksum          text NOT NULL,
  approved_by       text NOT NULL,       -- human approver of the config, not the AI
  approved_at       timestamptz NOT NULL,
  UNIQUE (regime, version)
);

CREATE TABLE rule (
  id            uuid PRIMARY KEY,
  ruleset_id    uuid REFERENCES ruleset(id),
  key           text NOT NULL,           -- 'SVP_THRESHOLD', 'POSTING_DAYS_COMPETITIVE'
  value_json    jsonb NOT NULL,          -- {"amount":2000000,"currency":"PHP"}
  section_ref   text NOT NULL,           -- 'IRR RA 12009 Sec. 34.1'
  mode          text,                    -- null = applies to all modes
  UNIQUE (ruleset_id, key, mode)
);

CREATE TABLE form_template (
  id            uuid PRIMARY KEY,
  ruleset_id    uuid REFERENCES ruleset(id),
  code          text NOT NULL,           -- 'RFQ_REI', 'NOTICE_OF_AWARD'
  classification text NOT NULL CHECK (classification IN ('STANDARD','RECOMMENDED')),
  version       text NOT NULL,
  file_hash     text,                   -- NULL until a human registers the official body
  verified      boolean NOT NULL DEFAULT false,
  effective_from date NOT NULL
);
-- GPPB Res. 03-2025: only Strategic Planning Forms, PBD and PBD Related Forms
-- are Standard Forms. NOA, Abstract of Bids, RFQ etc. are Recommended ("other forms").
-- A NULL file_hash means the system may emit only a labelled structural draft.

CREATE TABLE corpus_snapshot (
  id            uuid PRIMARY KEY,
  version       text NOT NULL,
  created_at    timestamptz NOT NULL,
  checksum      text NOT NULL,
  approved_by   text NOT NULL,
  chunk_count   int NOT NULL
);

CREATE TABLE transaction (
  id                  uuid PRIMARY KEY,
  number              text UNIQUE NOT NULL,
  regime              text NOT NULL,          -- IMMUTABLE (I1)
  ruleset_version     text NOT NULL,
  corpus_snapshot_id  uuid NOT NULL,
  mode                text NOT NULL,          -- one of 11 / legacy modes
  award_criterion     text,                  -- LCRB | MEARB | MARB | HRRB
  posting_date        date,
  stage               text NOT NULL,
  created_at          timestamptz NOT NULL
);
-- Enforce immutability with a trigger rejecting UPDATE of the pinned columns.

CREATE TABLE stage_gate (
  id              uuid PRIMARY KEY,
  transaction_id  uuid REFERENCES transaction(id),
  from_stage      text NOT NULL,
  to_stage        text NOT NULL,
  required_role   text NOT NULL,        -- 'BAC_CHAIR', 'HOPE', 'BUDGET_OFFICER'
  required_count  int DEFAULT 1,
  UNIQUE (transaction_id, from_stage, to_stage, required_role)
);

CREATE TABLE signoff (
  id              uuid PRIMARY KEY,
  transaction_id  uuid NOT NULL,
  gate_id         uuid REFERENCES stage_gate(id),
  signer_id       text NOT NULL,
  signer_role     text NOT NULL,
  method          text NOT NULL,        -- 'PNPKI' | 'WET_INK_SCAN'
  signature_hash  text NOT NULL,
  document_hash   text NOT NULL,
  signed_at       timestamptz NOT NULL,
  UNIQUE (gate_id, signer_id, document_hash)
);

CREATE TABLE ledger_event (
  seq             bigserial PRIMARY KEY,   -- gap-free, DB-enforced
  event_id        uuid UNIQUE NOT NULL,
  transaction_id  uuid NOT NULL,
  event_type      text NOT NULL,
  actor_id        text NOT NULL,
  actor_role      text NOT NULL,
  payload         jsonb NOT NULL,
  occurred_at     timestamptz NOT NULL,
  prev_hash       text NOT NULL,
  row_hash        text NOT NULL           -- sha256(prev_hash || canonical(payload) || ...)
);
-- Revoke UPDATE/DELETE on ledger_event from the application role.
```

---

## 4. Canonical payloads

### 4.1 Verdict (produced by the checker, consumed by humans)

```json
{
  "transaction_id": "…",
  "bidder_id": "…",
  "document_id": "…",
  "input_doc_hash": "sha256:…",
  "status": "ABSTAIN",
  "missing_documents": [],
  "invalid_documents": [],
  "notes": ["Illegible notarization seal on page 4"],
  "evidence_spans": [
    {"field": "omnibus_sworn_statement.notarized", "page": 4, "bbox": [..], "snippet": "…"}
  ],
  "rule_citations": [
    {"section": "IRR RA 12009 Sec. 59.1", "chunk_id": "…", "passage_hash": "sha256:…"}
  ],
  "confidence": 0.61,
  "decision": "ABSTAIN",
  "model_version": "qwen2.5-3b-ft-2026-10-01",
  "ruleset_version": "RA12009_IRR@2025-02",
  "corpus_snapshot_id": "…",
  "prompt_hash": "sha256:…",
  "advisory": true
}
```

**Rule:** `decision ∈ {PASS, FAIL, ABSTAIN}`. `ABSTAIN` is mandatory when confidence < calibrated threshold, a citation fails verification, or the schema cannot be validated. `advisory` is always `true` — only a `signoff` event creates an official determination.

### 4.2 Ledger event

```json
{
  "event_type": "BID_OPENING_RECORDED",
  "transaction_id": "…",
  "actor_id": "bac-chair-01",
  "actor_role": "BAC_CHAIR",
  "payload": {
    "opening_at": "2026-11-05T10:03:00+08:00",
    "minutes_ref": "…",
    "envelopes_opened": ["technical"],
    "financial_envelopes_sealed": true,
    "attendees": ["…"],
    "password_disclosed_by_bidders": true
  }
}
```

---

## 5. API contracts

| Endpoint | Purpose | Key behaviour |
|---|---|---|
| `POST /rules/resolve` | `{as_of, mode, legacy_ref?, override?}` → `{regime, ruleset_version, corpus_snapshot_id, rules[]}` | Deterministic; override requires a recorded `legal_officer` signoff + reason |
| `POST /transactions` | Create transaction, pin all three identifiers | Rejects if regime cannot be resolved unambiguously |
| `POST /numbers/issue` | `{type, unit, year}` → number | `SELECT ... FOR UPDATE` / DB sequence; atomic, gap-free, logged |
| `POST /numbers/void` | `{number, reason, approver}` | Marks void; does not delete; preserves sequence |
| `POST /gates/{tx}/transition` | Request stage transition | Returns `409` with missing signoffs if any gate unsatisfied; emits `GATE_BLOCKED` event |
| `POST /events` | Append ledger event | Append-only; rejects duplicate `event_id` (idempotent); returns `seq` and `row_hash` |
| `POST /documents/ingest` | Ingest a document | **Rejects with 403 if `artifact_class = BID_CONTENT` and no `BID_OPENING_RECORDED` event exists** (I3) |
| `POST /retrieve` | `{query, as_of, regime, snapshot_id}` | Hybrid BM25+dense, filtered by effective dates; returns chunks + `passage_hash` |
| `POST /citations/verify` | `{assertion, chunk_ids[]}` | Checks retrieved passage supports the assertion; failure forces `ABSTAIN` |
| `GET /audit/{tx}/folder` | Materialized COA-ready folder | Read-only view over ledger + documents; includes hash manifest |
| `GET /audit/verify-chain` | `{from_seq,to_seq}` → integrity report | Runs outside the application role; usable by COA |

---

## 6. Regime resolution algorithm

```
resolve_rules(as_of, mode, legacy_ref, override):
  1. if override present: require legal_officer signoff + reason; log override event; continue
  2. candidates = rulesets where effective_from <= as_of
                  and (effective_to is null or as_of < effective_to)
  3. if legacy_ref present and as_of < IRR_EFFECTIVITY:
         regime = RA9184_2016IRR
     else if as_of >= IRR_EFFECTIVITY:
         regime = RA12009_IRR
     else: return AMBIGUOUS (fail closed, requires legal officer)
  4. if zero or >1 matching ruleset for the regime: fail closed + alert
  5. return {regime, ruleset_version, active corpus_snapshot_id, rules for mode}
```

**Transition handling:** pending GPPB guidance on which law applies during the transitory period is itself a config value (`TRANSITION_POLICY`), not code. When the issuance arrives, add a new `ruleset` row and a new `corpus_snapshot`; existing transactions keep their pinned values. A transaction posted under one regime and awarded under another is flagged `CROSS_REGIME_REVIEW` and blocked until a legal officer signs off.

---

## 7. Custody gate (the sealed-bid boundary)

**States:** `RECEIVING → CUSTODY_LOGGED → OPENING_SCHEDULED → BID_OPENING_RECORDED → CONTENT_INGESTION → EVALUATION → …`

| From | To | Precondition |
|---|---|---|
| `RECEIVING` | `CUSTODY_LOGGED` | Receipt record has bidder, timestamp, envelope count, seal condition; **no content** |
| `CUSTODY_LOGGED` | `OPENING_SCHEDULED` | Time/place per bidding documents; deadline not yet passed |
| `OPENING_SCHEDULED` | `BID_OPENING_RECORDED` | BAC Chair signoff + minutes reference + attendee list; recorded in ledger |
| `BID_OPENING_RECORDED` | `CONTENT_INGESTION` | Ledger contains the opening event; envelope scope matches award criterion |

**Forbidden transitions (enforced in DB + API, not just UI):**

- Any `documents/ingest` with `artifact_class = BID_CONTENT` before `BID_OPENING_RECORDED` → `403`.
- Any vector-index or embedding job touching bid documents before the opening event → blocked by a worker-level policy check.
- Any `FINANCIAL_ENVELOPE_OPEN` before the second opening event when `award_criterion ∈ {MEARB, MARB}` (IRR Sec. 58.2(b)) → `409`.
- Storing an e-bid password anywhere in the system → schema has no column; ingestion rejects `password` fields (Sec. 58.3).
- Late bids are logged and marked `RETURN_UNOPENED`; no ingestion permitted.

**Implementation note:** document objects created at receipt are metadata-only records with a sealed content pointer that the storage layer refuses to dereference until the opening event exists. This makes the control a storage/authorization property, not a convention the application might forget.

---

## 8. Human gate enforcement

```
can_transition(tx, from, to):
  gates   = stage_gate where (transaction_id, from_stage, to_stage)
  for each gate:
     valid = signoff where gate_id = gate.id
             and document_hash = current_document_hash(tx)
             and signature verifies
             and signer_role = gate.required_role
             and signer_id != drafter_id(tx)          -- segregation of duties
     if count(distinct signer_id) < gate.required_count: return BLOCKED
  append STAGE_TRANSITION event with all signoff ids
```

- Signature verification: PNPKI/RA 8792 digital signature, or a wet-ink scan with a witnessed signoff event recording signer identity, document hash and timestamp. Choose one with the legal officer; do not allow both ad hoc.
- Every gate attempt — successful or blocked — is a ledger event (`GATE_PASSED` / `GATE_BLOCKED`) with the actor and reason.
- A signoff is bound to a `document_hash`. If the AI draft is edited after signing, the signoff is invalidated and a new one is required. This is what prevents "pre-signed" BAC resolutions.
- AI drafts carry a permanent `AI_DRAFTED` marker; the ledger records `AI_DRAFT_CREATED` and `HUMAN_EDIT_APPLIED` separately for audit transparency.

---

## 9. As-of retrieval and citation verification

**Chunk metadata (mandatory):** `law`, `issuance_no`, `section`, `regime`, `effective_from`, `effective_to`, `supersedes`, `source_url`, `checksum`, `corpus_snapshot_id`, `chunk_id`, `passage_hash`.

**Query:** `filter(regime = tx.regime AND effective_from <= tx.posting_date AND (effective_to IS NULL OR tx.posting_date < effective_to) AND corpus_snapshot_id = tx.corpus_snapshot_id)` → hybrid BM25 + dense (multilingual embeddings) → re-rank by section-number exact match → top-k with `passage_hash`.

**Verification:** a lightweight entailment check (rules-based + model-assisted) confirms the cited passage supports the asserted requirement. On mismatch → verdict `ABSTAIN`, log `CITATION_MISMATCH`. Never show a flag without a verified passage.

**Corpus updates:** new GPPB issuance → ingest → build candidate snapshot → human approves → activate. Active transactions are never re-pinned. Retrieval accuracy (correct section retrieved) is measured separately from generation accuracy (correct use of the passage).

---

## 10. Ledger integrity and COA export

- `row_hash = sha256(prev_hash || canonical_json(payload) || event_id || occurred_at || actor_id)`. Genesis hash stored offline.
- Application DB role: `INSERT` only on `ledger_event`; `UPDATE`/`DELETE` revoked. Sequence gaps detectable via `seq`.
- Hourly chain verification job alerts on mismatch; daily signed manifest (from/to seq + root hash) written to WORM storage.
- `GET /audit/verify-chain` runs under a separate read-only role so COA can verify without trusting the application.
- Audit folder = read-only materialization of all events + document hashes + signoffs + verdicts + rule/corpus versions for one transaction, with a hash manifest sealed at archiving. This is the deliverable that answers "which version of the model and which rule produced this flag, and who approved it".

---

## 11. Local → VPS migration

1. All services containerized; configuration via env vars; no hardcoded host paths.
2. Postgres logical dump + vector collection export + object-store sync; document hashes recomputed and compared.
3. Verify hash chain on the target before cutover; record `MIGRATION_VERIFIED` event (this itself is an auditable event on the new host).
4. Dual-read period; then cutover with a recorded downtime window. Never rebuild the ledger.
5. Re-run the full acceptance suite on the VPS, including the custody-gate negative tests.
6. No external model/embedding API at either site; both inference and retrieval run in-container.

---

## 12. Acceptance criteria (testable)

| # | Test | Expected |
|---|---|---|
| A1 | Resolve rules for a 2023 posting date | `RA9184_2016IRR`, correct ruleset; never RA 12009 |
| A2 | Resolve rules for a 2026 posting date | `RA12009_IRR`, current ruleset + pinned snapshot |
| A3 | Add a new GPPB ruleset mid-flight | Existing transaction verdicts reproduce identically; new transactions use new rules |
| A4 | Attempt `documents/ingest` of bid content before opening | `403`, `POLICY_BLOCKED` event written, no bytes stored |
| A5 | Record opening, then ingest | Succeeds; `CONTENT_INGESTED` event references the opening `seq` |
| A6 | MEARB transaction: attempt financial-envelope open before second opening | `409` |
| A7 | Transition without a required signoff | `409 GATE_BLOCKED`, no stage change |
| A8 | Edit a signed AI draft and attempt transition | Signoff invalidated; new signature required |
| A9 | Same signer as drafter | Rejected (segregation of duties) |
| A10 | Verdict with confidence below threshold / unverifiable citation | `ABSTAIN`, routed to human queue, no official determination |
| A11 | Tamper with a ledger row directly | Chain verification fails and alerts |
| A12 | Replay a verdict from stored identifiers | Byte-identical verdict object |
| A13 | Retrieve a rule repealed before the transaction date | Chunk filtered out; verified citation unavailable → `ABSTAIN` |
| A14 | Numbering under 100 concurrent requests | No duplicates, no gaps, every issuance logged |
| A15 | Migrate local → VPS | Hash chain verifies on target; all A1–A14 pass again |
| A16 | Extraction attempted before `BID_OPENING_RECORDED` | `POLICY_BLOCKED`, no extraction, no verdict |
| A17 | Model returns a hallucinated field / wrong type / unknown document | `ABSTAIN` verdict + `SCHEMA_VALIDATION_FAILED` event, never FAIL |
| A18 | Per-field confidence below threshold | Field omitted; checker abstains on that field; document not listed as missing |
| A19 | Configure an external (non-loopback) model endpoint | Refused at construction; no network call made |
| A20 | Model server unavailable | `EXTRACTION_FAILED` event and **no verdict**; verdict records adapter-computed `prompt_hash` and both model/engine versions on success |

---

## 13. Failure modes

| Failure | Behaviour |
|---|---|
| Rules registry unavailable | Fail closed: no transaction creation, no verdicts, no transitions |
| No matching ruleset for a date | `AMBIGUOUS` → legal officer queue; never default to "latest" |
| Corpus snapshot missing/corrupt | Verdicts degrade to `ABSTAIN`; never fall back to an unpinned corpus |
| Signature service down | Gates blocked (not bypassed); manual wet-ink fallback requires a recorded exception |
| Model unavailable | `EXTRACTION_FAILED` event; no verdict generated; no silent rule-engine guess from missing fields |
| Model returns malformed / hallucinated JSON | Strict schema validation fails → `ABSTAIN` verdict, human queue |
| OCR unreadable / low-confidence field | `ABSTAIN`, never a FAIL that could disqualify a bidder |
| Non-loopback model URL configured | Refused by the adapter before any network call |
| Clock drift | Server time from a single NTP-synced source; `occurred_at` and `signed_at` validated against it |
