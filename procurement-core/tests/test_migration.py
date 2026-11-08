"""Tests for the PostgreSQL migration scaffold (schema_pg.sql, ops_migrate_pg.py).

- schema parity: schema_pg.sql declares the same tables/columns as schema.sql
  and as a live-initialized DB
- export: deterministic, FK-ordered, exact seq/hash/float/unicode/quote
  preservation, identity setval
- round-trip: dump INSERTs replay into a scratch SQLite DB and the hash chain
  verifies (check-dump)
- fail closed: tampered ledger row, FK violation, unsupported values
- iter_insert_statements: quote-aware statement splitting
"""

import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import append_event, create_transaction, ingest_document, init_db
from ops_migrate_pg import (
    MigrationError, _tables, build_dump, check_dump, iter_insert_statements,
    main, verify_migrate_source,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_SQL = os.path.join(BASE_DIR, "schema.sql")
SCHEMA_PG_SQL = os.path.join(BASE_DIR, "schema_pg.sql")

# A note that stresses escaping: single quotes, unicode, semicolon, newline.
NASTY_NOTE = "Legal note: BAC 'ok' — ₱1,500,000;\nverbatim quote retained"
FIXED_TS = "2026-11-08T00:00:00+08:00"


# ------------------------------------------------------------------ helpers

_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", re.S)
_CONSTRAINT_KEYWORDS = {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}


def _parse_schema_tables(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    tables = {}
    for name, body in _TABLE_RE.findall(text):
        cols = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            first = line.split()[0]
            if first in _CONSTRAINT_KEYWORDS:
                continue
            cols.append(first.rstrip(","))
        tables[name] = cols
    return tables


def make_source_db(path):
    """A small but real DB: seeds + a transaction, documents, events, floats."""
    conn = init_db(path)  # seed=True: rules/forms/corpus/transition_policy
    tx_id = create_transaction(
        conn, regime="RA12009_IRR", posting_date="2026-11-05",
        mode="Competitive Bidding", award_criterion="LCRB",
        drafter_id="staff-01", legal_basis_note=NASTY_NOTE)
    ingest_document(conn, tx_id, "SUPPORTING_DOC", "b64-content-unused",
                    {"filename": "notes–§3.pdf", "pages": 4}, actor_id="staff-01")
    append_event(conn, tx_id, "TEST_EVENT", "bac-secretariat-01", "SECRETARIAT",
                 {"note": "it''s a quote; with ₱ and\ntext"})
    conn.execute(
        "INSERT INTO contract_terms (tx_id, contract_amount, start_date, "
        "completion_date, ld_rate_per_day, ld_cap_ratio, approved_by, approved_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tx_id, 1500000.0, "2026-12-01", "2027-05-31", 1500.0, 0.1,
         "hope-01", "2026-11-06"))
    conn.commit()
    return conn, tx_id


def replay_into_scratch(dump_text):
    """Replay dump INSERTs into a fresh SQLite schema (dialect-neutral SQL)."""
    replay = init_db(":memory:", seed=False)
    for stmt in iter_insert_statements(dump_text):
        replay.execute(stmt)
    return replay


# ------------------------------------------------------------------- parity

class SchemaParityTests(unittest.TestCase):
    """schema_pg.sql must stay in lockstep with schema.sql / the live schema."""

    def test_schema_files_declare_same_tables_and_columns(self):
        sq = _parse_schema_tables(SCHEMA_SQL)
        pg = _parse_schema_tables(SCHEMA_PG_SQL)
        self.assertEqual(set(sq), set(pg),
                         "schema.sql and schema_pg.sql table sets differ")
        for name in sq:
            self.assertEqual(sq[name], pg[name],
                             f"column mismatch in table {name}")

    def test_schema_pg_matches_live_database(self):
        pg = _parse_schema_tables(SCHEMA_PG_SQL)
        conn = init_db(":memory:")
        try:
            live = {t: [r["name"] for r in
                        conn.execute(f"PRAGMA table_info({t})").fetchall()]
                    for t in _tables(conn)}
        finally:
            conn.close()
        self.assertEqual(set(pg), set(live))
        for name, cols in live.items():
            self.assertEqual(pg[name], cols, f"live column mismatch in {name}")


# ------------------------------------------------------------- export tests

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="procemp-pg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "procurement.db")
        self.conn, self.tx_id = make_source_db(self.db_path)

    def tearDown(self):
        self.conn.close()

    def _dump(self):
        dump, report, total = build_dump(
            self.conn, generated_at=FIXED_TS, source_label="procurement.db",
            source_sha256="0" * 64)
        return dump, report, total

    def test_source_verifies_before_export(self):
        report = verify_migrate_source(self.conn)
        self.assertTrue(report["ok"], report["problems"])
        self.assertGreater(report["ledger"]["events"], 2)

    def test_dump_is_deterministic(self):
        self.assertEqual(self._dump()[0], self._dump()[0])

    def test_all_tables_present_with_counts(self):
        dump, report, total = self._dump()
        markers = dict(re.findall(
            r"-- table: (\w+) \((\d+) rows\)", dump))
        live = {t: report["counts"][t] for t in _tables(self.conn)}
        self.assertEqual(set(markers), set(live))
        for t, n in live.items():
            self.assertEqual(int(markers[t]), n, f"row count marker for {t}")
        self.assertEqual(total, sum(live.values()))

    def test_escapes_quotes_and_preserves_unicode(self):
        dump, _, _ = self._dump()
        self.assertIn("it''''s a quote", dump)         # both quotes doubled
        self.assertIn("BAC ''ok''", dump)              # doubled inner quote
        self.assertIn("₱1,500,000", dump)              # unicode preserved
        self.assertIn("notes–§3.pdf", dump)            # unicode metadata
        # statement splitter must reconstruct the nasty note byte-exactly
        replay = replay_into_scratch(dump)
        try:
            row = replay.execute(
                "SELECT legal_basis_note FROM proc_tx WHERE id=?",
                (self.tx_id,)).fetchone()
            self.assertEqual(row[0], NASTY_NOTE)
            terms = replay.execute(
                "SELECT contract_amount, ld_cap_ratio FROM contract_terms "
                "WHERE tx_id=?", (self.tx_id,)).fetchone()
            self.assertEqual((terms[0], terms[1]), (1500000.0, 0.1))
        finally:
            replay.close()

    def test_roundtrip_replay_verifies_chain(self):
        dump, _, _ = self._dump()
        result = check_dump(self.conn, dump)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["counts_match"])
        self.assertTrue(result["replay_chain_ok"])

    def test_ledger_seq_and_setval_preserved(self):
        dump, _, _ = self._dump()
        max_seq = self.conn.execute(
            "SELECT MAX(seq) FROM ledger_event").fetchone()[0]
        seqs = re.findall(
            r"INSERT INTO ledger_event \(seq, .*? VALUES \((\d+),", dump)
        self.assertEqual(sorted(int(s) for s in seqs),
                         [r[0] for r in self.conn.execute(
                             "SELECT seq FROM ledger_event ORDER BY seq")])
        self.assertIn(
            f"SELECT setval(pg_get_serial_sequence('ledger_event', 'seq'), "
            f"{max_seq}, true);", dump)

    def test_fk_safe_insert_order(self):
        dump, _, _ = self._dump()
        pos = {name: dump.index(f"-- table: {name} ")
               for name in ("ruleset", "rule", "corpus_snapshot", "corpus_chunk",
                            "proc_tx", "stage_gate", "signoff", "document",
                            "form_registry", "draft", "review_item")}
        for parent, child in (("ruleset", "rule"),
                              ("corpus_snapshot", "corpus_chunk"),
                              ("proc_tx", "stage_gate"),
                              ("stage_gate", "signoff"),
                              ("proc_tx", "document"),
                              ("form_registry", "draft"),
                              ("proc_tx", "review_item")):
            self.assertLess(pos[parent], pos[child],
                            f"{parent} must precede {child} in the dump")

    def test_fail_closed_on_tampered_ledger_row(self):
        self.conn.execute(
            "INSERT INTO ledger_event (event_id, tx_id, event_type, actor_id, "
            "actor_role, payload, occurred_at, prev_hash, row_hash) "
            "VALUES ('bad-ev', 'no-such-tx', 'TAMPERED', 'x', 'IT', '{}', "
            "'2026-11-08T00:00:00+08:00', 'prev', 'not-a-real-hash')")
        self.conn.commit()
        report = verify_migrate_source(self.conn)
        self.assertFalse(report["ok"])
        self.assertIn("ledger hash chain broken", " ".join(report["problems"]))
        with self.assertRaises(MigrationError):
            build_dump(self.conn, generated_at=FIXED_TS)
        # no cleanup: ledger rows cannot be deleted (append-only trigger);
        # each test gets a fresh DB from setUp

    def test_fail_closed_on_fk_violation(self):
        raw = sqlite3.connect(self.db_path)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute(
                "INSERT INTO document (id, tx_id, artifact_class, content_hash, "
                "metadata, ingested_at) VALUES ('orphan', 'no-such-tx', 'X', "
                "'h', '{}', '2026-11-08T00:00:00+08:00')")
            raw.commit()
        finally:
            raw.close()
        report = verify_migrate_source(self.conn)
        self.assertFalse(report["ok"])
        self.assertTrue(any("foreign-key violations" in p
                            for p in report["problems"]))
        self.conn.execute("DELETE FROM document WHERE id='orphan'")
        self.conn.commit()
        self.assertTrue(verify_migrate_source(self.conn)["ok"])

    def test_cli_export_and_check_dump_end_to_end(self):
        out_path = os.path.join(self.tmp, "pg-migration.sql")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["export", "--db", self.db_path,
                                   "--out", out_path]), 0)
        self.assertTrue(os.path.exists(out_path))
        summary = json.loads(buf.getvalue())
        self.assertEqual(summary["tables"], len(_tables(self.conn)))
        self.assertTrue(summary["ok"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["check-dump", "--db", self.db_path,
                                   "--dump", out_path]), 0)
        result = json.loads(buf.getvalue())
        self.assertTrue(result["ok"], result)
        with open(out_path, encoding="utf-8") as fh:
            self.assertIn("BEGIN;", fh.read())

    def test_cli_verify_source_ok_exit_code(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["verify-source", "--db", self.db_path]), 0)


# ------------------------------------------------------- splitter unit tests

class IterInsertStatementsTests(unittest.TestCase):
    def test_marker_comment_before_statement_is_dropped(self):
        # Regression: a "-- table: ..." marker must not contaminate or
        # swallow the INSERT that follows it.
        text = "-- table: t (1 rows)\nINSERT INTO t (a) VALUES ('v');\n"
        self.assertEqual(list(iter_insert_statements(text)),
                         ["INSERT INTO t (a) VALUES ('v')"])

    def test_escaped_quote_and_semicolon_inside_string(self):
        text = ("INSERT INTO t (a) VALUES ('it''s; tricky');\n"
                "INSERT INTO u (b) VALUES ('x');\n"
                "-- comment; with semicolon\n")
        stmts = list(iter_insert_statements(text))
        self.assertEqual(stmts, [
            "INSERT INTO t (a) VALUES ('it''s; tricky')",
            "INSERT INTO u (b) VALUES ('x')",
        ])

    def test_multiline_statement_preserved(self):
        text = "INSERT INTO t (a) VALUES ('line1;\nline2');\n"
        self.assertEqual(list(iter_insert_statements(text)),
                         ["INSERT INTO t (a) VALUES ('line1;\nline2')"])

    def test_non_insert_statements_skipped(self):
        text = "SELECT 1;\nBEGIN;\nCOMMIT;\nINSERT INTO t (a) VALUES ('v');\n"
        self.assertEqual(list(iter_insert_statements(text)),
                         ["INSERT INTO t (a) VALUES ('v')"])


if __name__ == "__main__":
    unittest.main()
