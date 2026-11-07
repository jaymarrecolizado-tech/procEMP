PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ruleset (
  id             TEXT PRIMARY KEY,
  regime         TEXT NOT NULL,
  version        TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to   TEXT,
  issuance_ref   TEXT NOT NULL,
  checksum       TEXT NOT NULL,
  approved_by    TEXT NOT NULL,
  approved_at    TEXT NOT NULL,
  note           TEXT,
  UNIQUE (regime, version)
);

CREATE TABLE IF NOT EXISTS rule (
  id          TEXT PRIMARY KEY,
  ruleset_id  TEXT NOT NULL REFERENCES ruleset(id),
  key         TEXT NOT NULL,
  value_json  TEXT NOT NULL,
  section_ref TEXT NOT NULL,
  mode        TEXT NOT NULL DEFAULT '*',
  verified    INTEGER NOT NULL DEFAULT 0,
  note        TEXT,
  UNIQUE (ruleset_id, key, mode)
);

CREATE TABLE IF NOT EXISTS corpus_snapshot (
  id          TEXT PRIMARY KEY,
  version     TEXT NOT NULL,
  regime      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  checksum    TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  is_active   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS corpus_chunk (
  chunk_id            TEXT PRIMARY KEY,
  corpus_snapshot_id  TEXT NOT NULL REFERENCES corpus_snapshot(id),
  law                 TEXT NOT NULL,
  issuance_no         TEXT NOT NULL,
  section             TEXT NOT NULL,
  regime              TEXT NOT NULL,
  effective_from      TEXT NOT NULL,
  effective_to        TEXT,
  supersedes          TEXT,
  source_url          TEXT NOT NULL,
  text                TEXT NOT NULL,
  passage_hash        TEXT NOT NULL,
  verified            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transition_policy (
  id     INTEGER PRIMARY KEY CHECK (id = 1),
  status TEXT NOT NULL,
  note   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proc_tx (
  id                 TEXT PRIMARY KEY,
  number             TEXT UNIQUE NOT NULL,
  regime             TEXT NOT NULL,
  ruleset_id         TEXT NOT NULL REFERENCES ruleset(id),
  ruleset_version    TEXT NOT NULL,
  corpus_snapshot_id TEXT NOT NULL REFERENCES corpus_snapshot(id),
  mode               TEXT NOT NULL,
  award_criterion    TEXT,
  posting_date       TEXT NOT NULL,
  stage              TEXT NOT NULL,
  drafter_id         TEXT,
  legal_basis_note   TEXT,
  created_at         TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS proc_tx_no_repin
BEFORE UPDATE OF regime, ruleset_id, ruleset_version, corpus_snapshot_id, posting_date ON proc_tx
BEGIN
  SELECT RAISE(ABORT, 'pinned transaction fields are immutable');
END;

CREATE TABLE IF NOT EXISTS stage_gate (
  id             TEXT PRIMARY KEY,
  tx_id          TEXT NOT NULL REFERENCES proc_tx(id),
  from_stage     TEXT NOT NULL,
  to_stage       TEXT NOT NULL,
  required_role  TEXT NOT NULL,
  required_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE (tx_id, from_stage, to_stage, required_role)
);

CREATE TABLE IF NOT EXISTS signoff (
  id             TEXT PRIMARY KEY,
  tx_id          TEXT NOT NULL REFERENCES proc_tx(id),
  gate_id        TEXT NOT NULL REFERENCES stage_gate(id),
  signer_id      TEXT NOT NULL,
  signer_role    TEXT NOT NULL,
  method         TEXT NOT NULL,
  signature_hash TEXT NOT NULL,
  document_hash  TEXT NOT NULL,
  signed_at      TEXT NOT NULL,
  UNIQUE (gate_id, signer_id, document_hash)
);

CREATE TABLE IF NOT EXISTS ledger_event (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id    TEXT UNIQUE NOT NULL,
  tx_id       TEXT NOT NULL,
  event_type  TEXT NOT NULL,
  actor_id    TEXT NOT NULL,
  actor_role  TEXT NOT NULL,
  payload     TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  prev_hash   TEXT NOT NULL,
  row_hash    TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger_event
BEGIN
  SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger_event
BEGIN
  SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TABLE IF NOT EXISTS number_sequence (
  type       TEXT NOT NULL,
  unit       TEXT NOT NULL,
  year       INTEGER NOT NULL,
  next_value INTEGER NOT NULL,
  PRIMARY KEY (type, unit, year)
);

CREATE TABLE IF NOT EXISTS issued_number (
  number        TEXT PRIMARY KEY,
  type          TEXT NOT NULL,
  unit          TEXT NOT NULL,
  year          INTEGER NOT NULL,
  ordinal       INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'ACTIVE',
  issued_at     TEXT NOT NULL,
  void_reason   TEXT,
  void_approver TEXT,
  UNIQUE (type, unit, year, ordinal)
);

CREATE TABLE IF NOT EXISTS document (
  id             TEXT PRIMARY KEY,
  tx_id          TEXT NOT NULL REFERENCES proc_tx(id),
  artifact_class TEXT NOT NULL,
  content_hash   TEXT NOT NULL,
  metadata       TEXT NOT NULL,
  ingested_at    TEXT NOT NULL,
  opening_seq    INTEGER
);

CREATE TABLE IF NOT EXISTS verdict (
  id         TEXT PRIMARY KEY,
  tx_id      TEXT NOT NULL REFERENCES proc_tx(id),
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS form_registry (
  code                  TEXT PRIMARY KEY,
  name                  TEXT NOT NULL,
  classification        TEXT NOT NULL,
  category              TEXT NOT NULL,
  applies_to            TEXT NOT NULL,
  required_fields       TEXT NOT NULL,
  official_file_hash    TEXT,
  official_file_version TEXT,
  body_available        INTEGER NOT NULL DEFAULT 0,
  verified              INTEGER NOT NULL DEFAULT 0,
  source                TEXT NOT NULL,
  note                  TEXT
);

CREATE TABLE IF NOT EXISTS draft (
  id              TEXT PRIMARY KEY,
  tx_id           TEXT NOT NULL REFERENCES proc_tx(id),
  form_code       TEXT NOT NULL REFERENCES form_registry(code),
  body            TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  status          TEXT NOT NULL,
  approved_fields TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  created_by      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sealed_folder (
  id                  TEXT PRIMARY KEY,
  tx_id               TEXT NOT NULL REFERENCES proc_tx(id),
  version             INTEGER NOT NULL,
  folder_hash         TEXT NOT NULL,
  attested_chain_head TEXT NOT NULL,
  attested_last_seq   INTEGER NOT NULL,
  content_json        TEXT NOT NULL,
  sealed_at           TEXT NOT NULL,
  sealed_by           TEXT NOT NULL,
  supersedes_seal_id  TEXT,
  supersede_reason    TEXT,
  UNIQUE (tx_id, version)
);

CREATE TRIGGER IF NOT EXISTS sealed_folder_no_update
BEFORE UPDATE ON sealed_folder
BEGIN
  SELECT RAISE(ABORT, 'sealed folders are immutable; create a superseding version');
END;

CREATE TRIGGER IF NOT EXISTS sealed_folder_no_delete
BEFORE DELETE ON sealed_folder
BEGIN
  SELECT RAISE(ABORT, 'sealed folders are immutable');
END;

CREATE TABLE IF NOT EXISTS contract_terms (
  tx_id            TEXT PRIMARY KEY REFERENCES proc_tx(id),
  contract_amount  REAL NOT NULL,
  start_date       TEXT NOT NULL,
  completion_date  TEXT NOT NULL,
  ld_rate_per_day  REAL,
  ld_cap_ratio     REAL,
  approved_by      TEXT NOT NULL,
  approved_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestone (
  id              TEXT PRIMARY KEY,
  tx_id           TEXT NOT NULL REFERENCES proc_tx(id),
  sequence        INTEGER NOT NULL,
  name            TEXT NOT NULL,
  due_date        TEXT NOT NULL,
  amount          REAL,
  status          TEXT NOT NULL DEFAULT 'PENDING',
  completed_at    TEXT,
  evidence_doc_id TEXT,
  verified_by     TEXT,
  UNIQUE (tx_id, sequence)
);

CREATE TABLE IF NOT EXISTS variation_order (
  id               TEXT PRIMARY KEY,
  tx_id            TEXT NOT NULL REFERENCES proc_tx(id),
  reference        TEXT NOT NULL,
  description      TEXT NOT NULL,
  requested_amount REAL,
  reason           TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'DRAFT',
  requested_by     TEXT NOT NULL,
  reviewed_by      TEXT,
  reviewed_at      TEXT,
  created_at       TEXT NOT NULL,
  UNIQUE (tx_id, reference)
);
