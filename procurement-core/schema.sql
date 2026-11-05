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
