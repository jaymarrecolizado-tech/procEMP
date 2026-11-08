"""SQLite -> PostgreSQL migration scaffold (stdlib-only dump generator).

ProcEMP stays stdlib-only by design (see ops_backup.py): Python's standard
library has no PostgreSQL driver. This module therefore provides the
code-side half of the SQLite -> PostgreSQL migration as a deterministic,
PostgreSQL-compatible SQL dump generator, plus fail-closed verification:

- verify-source: ledger hash chain, corpus passage hashes, FK integrity and
  per-table row counts on the source DB (export refuses to run on failure)
- export: schema_pg.sql + INSERT statements (FK-safe topological order,
  deterministic row order, exact seq/hash/float preservation) + identity
  setval + a post-load verification manifest
- check-dump: replays a dump into a scratch SQLite database (the INSERT
  statements are dialect-neutral) and re-verifies counts + hash chain

Loading the dump into PostgreSQL (`psql -1 -v ON_ERROR_STOP=1 -f ...`) and
re-verifying there is an operator step on the approved target environment —
see procurement-review/06-deployment-runbook.md §9. The dump preserves
seq/row_hash byte-exactly so the chain can be re-verified on the target.

  python ops_migrate_pg.py verify-source --db procurement.db
  python ops_migrate_pg.py export --db procurement.db --out pg-migration.sql
  python ops_migrate_pg.py check-dump --db procurement.db --dump pg-migration.sql
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os

from core import CoreError, _connect, canonical, now, verify_chain
from rag import verify_corpus_integrity

SCHEMA_PG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "schema_pg.sql")

LEDGER_TABLE = "ledger_event"
IDENTITY_SEQ_COLUMN = "seq"


class MigrationError(CoreError):
    """Raised when verification fails — migration refuses to proceed."""


# ------------------------------------------------------------- introspection

def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def _columns(conn, table):
    return [r["name"] for r in
            conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _pk_cols(conn, table):
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in sorted((r for r in info if r["pk"]),
                                      key=lambda r: r["pk"])]


def _fk_parents(conn, table):
    """Tables referenced by this table's foreign keys."""
    parents = set()
    for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        parent = row["table"]
        if parent and parent != table:
            parents.add(parent)
    return parents


def _topo_order(conn, tables):
    """Deterministic FK-safe order: dependencies first, alphabetical ties."""
    remaining = {t: _fk_parents(conn, t) & set(tables) for t in tables}
    ordered, done = [], set()
    while remaining:
        ready = sorted(t for t, deps in remaining.items() if deps <= done)
        if not ready:
            raise MigrationError(
                "circular foreign-key dependency among: "
                + ", ".join(sorted(remaining)))
        for t in ready:
            ordered.append(t)
            done.add(t)
            del remaining[t]
    return ordered


# ---------------------------------------------------------------- rendering

def _render_value(value, table, column):
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # defensive: SQLite never yields bool
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MigrationError(
                f"non-finite float in {table}.{column}: {value!r}")
        return repr(value)  # round-trip exact for IEEE doubles
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise MigrationError(
        f"unsupported value type {type(value).__name__} in {table}.{column}")


def iter_insert_statements(dump_text):
    """Yield INSERT statements from a dump, quote-aware ('' escapes).

    The INSERT statements are dialect-neutral SQL, which is what lets
    check-dump replay them into a scratch SQLite database.
    """
    n = len(dump_text)
    buf, in_string = [], False
    i = 0
    while i < n:
        ch = dump_text[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                if dump_text[i + 1:i + 2] == "'":
                    buf.append("'")  # keep the escaped partner
                    i += 2           # consume both characters
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            # Drop leading comment lines (e.g. "-- table: x (n rows)" markers)
            # that accumulated after the previous statement's terminator.
            while stmt.startswith("--") and "\n" in stmt:
                stmt = stmt.split("\n", 1)[1].strip()
            if stmt.startswith("INSERT INTO"):
                yield stmt
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1


# ------------------------------------------------------------- verification

def verify_migrate_source(conn):
    """Fail-closed pre-migration checks: chain, corpus, FKs, counts."""
    problems = []
    counts = {}
    try:
        chain = verify_chain(conn)
    except Exception as exc:  # pragma: no cover - defensive reporting
        chain = {"ok": False, "error": str(exc), "head_hash": None}
    if not chain.get("ok"):
        problems.append(f"ledger hash chain broken: {chain}")
    corpus = verify_corpus_integrity(conn)
    if not corpus.get("ok"):
        problems.append(f"corpus passage hashes altered: {corpus}")
    fk_violations = [
        {"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]}
        for r in conn.execute("PRAGMA foreign_key_check").fetchall()
    ]
    if fk_violations:
        problems.append(f"foreign-key violations: {fk_violations}")
    for table in _tables(conn):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return {
        "ok": not problems,
        "problems": problems,
        "counts": counts,
        "ledger": {
            "events": counts.get(LEDGER_TABLE, 0),
            "chain_ok": bool(chain.get("ok")),
            "head_hash": chain.get("head_hash"),
        },
        "corpus_integrity_ok": bool(corpus.get("ok")),
        "checked_at": now(),
    }


# ------------------------------------------------------------------- export

def build_dump(conn, *, generated_at=None, schema_sql=None, source_label="",
               source_sha256=""):
    """Render the deterministic PostgreSQL dump. Raises MigrationError when
    source verification fails or a value cannot be represented exactly."""
    report = verify_migrate_source(conn)
    if not report["ok"]:
        raise MigrationError(
            "source DB failed pre-migration verification; refusing to export: "
            + "; ".join(report["problems"]))
    if schema_sql is None:
        with open(SCHEMA_PG_PATH, "r", encoding="utf-8") as fh:
            schema_sql = fh.read()
    tables = _topo_order(conn, _tables(conn))
    max_seq = conn.execute(
        f"SELECT MAX({IDENTITY_SEQ_COLUMN}) FROM {LEDGER_TABLE}").fetchone()[0]

    out = [
        "-- ProcEMP PostgreSQL migration dump (generated by ops_migrate_pg.py)",
        f"-- Generated: {generated_at or now()}",
        f"-- Source: {source_label} (sha256: {source_sha256 or 'n/a'})",
        f"-- Ledger: events={report['ledger']['events']} "
        f"head={report['ledger']['head_hash']} "
        f"chain_ok={str(report['ledger']['chain_ok']).lower()}",
        "-- Advisory: load on the approved target environment only; loading is an",
        "-- operator step — see procurement-review/06-deployment-runbook.md §9.",
        "",
        "\\set ON_ERROR_STOP on",
        "",
        "-- ============================ schema ============================",
        schema_sql.rstrip(),
        "",
        "-- ============================== data ============================",
        "BEGIN;",
    ]
    total_rows = 0
    for table in tables:
        cols = _columns(conn, table)
        collist = ", ".join(cols)
        order = (", ".join(_pk_cols(conn, table)) or "rowid")
        rows = conn.execute(
            f"SELECT {collist} FROM {table} ORDER BY {order}").fetchall()
        out.append(f"-- table: {table} ({len(rows)} rows)")
        for row in rows:
            values = ", ".join(
                _render_value(v, table, c) for v, c in zip(row, cols))
            out.append(f"INSERT INTO {table} ({collist}) VALUES ({values});")
        total_rows += len(rows)
    if max_seq is None:
        out.append(
            f"SELECT setval(pg_get_serial_sequence('{LEDGER_TABLE}', "
            f"'{IDENTITY_SEQ_COLUMN}'), 1, false);")
    else:
        out.append(
            f"SELECT setval(pg_get_serial_sequence('{LEDGER_TABLE}', "
            f"'{IDENTITY_SEQ_COLUMN}'), {max_seq}, true);")
    out.append("COMMIT;")

    count_selects = " UNION ALL ".join(
        f"SELECT '{t}' AS t, COUNT(*) FROM {t}" for t in tables)
    out += [
        "",
        "-- ================== post-load verification =====================",
        "-- Expected row counts (compare against source before switching):",
        f"-- {json.dumps(report['counts'], ensure_ascii=False, sort_keys=True)}",
        f"-- {count_selects};",
        "-- Ledger seq/row_hash are preserved byte-exactly; re-run application-",
        "-- level chain verification on the target once a driver-based verifier",
        "-- exists (external step). Never edit the dump by hand.",
        "",
    ]
    return "\n".join(out), report, total_rows


# --------------------------------------------------------------- check-dump

def check_dump(conn, dump_text):
    """Replay the dump into a scratch SQLite DB and re-verify counts+chain."""
    source = verify_migrate_source(conn)
    if not source["ok"]:
        raise MigrationError(
            "source DB failed verification: " + "; ".join(source["problems"]))
    from core import init_db
    replay = init_db(":memory:", seed=False)
    try:
        replayed = 0
        for stmt in iter_insert_statements(dump_text):
            replay.execute(stmt)
            replayed += 1
        counts = {t: replay.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in sorted(_tables(replay))}
        chain = verify_chain(replay)
        mismatches = {
            t: {"source": n, "replay": counts.get(t)}
            for t, n in source["counts"].items() if counts.get(t) != n
        }
        return {
            "ok": not mismatches and chain.get("ok"),
            "insert_statements": replayed,
            "counts_match": not mismatches,
            "count_mismatches": mismatches,
            "replay_chain_ok": bool(chain.get("ok")),
            "replay_head_hash": chain.get("head_hash"),
            "checked_at": now(),
        }
    finally:
        replay.close()


# ---------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="SQLite -> PostgreSQL migration scaffold (advisory, fail-closed)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify-source", help="Verify a source DB before migration")
    p.add_argument("--db", required=True)

    p = sub.add_parser("export", help="Export a PostgreSQL-compatible SQL dump")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--schema", default=SCHEMA_PG_PATH,
                   help="schema_pg.sql path (default: alongside this module)")

    p = sub.add_parser("check-dump", help="Replay a dump into scratch SQLite and verify")
    p.add_argument("--db", required=True)
    p.add_argument("--dump", required=True)

    args = parser.parse_args(argv)
    conn = _connect(args.db)
    try:
        if args.cmd == "verify-source":
            report = verify_migrate_source(conn)
            print(canonical(report))
            return 0 if report["ok"] else 1
        if args.cmd == "export":
            with open(args.db, "rb") as fh:
                source_sha = hashlib.sha256(fh.read()).hexdigest()
            with open(args.schema, "r", encoding="utf-8") as fh:
                schema_sql = fh.read()
            dump, report, total_rows = build_dump(
                conn, schema_sql=schema_sql,
                source_label=os.path.basename(args.db),
                source_sha256=source_sha)
            with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(dump)
            summary = {
                "ok": True,
                "out": args.out,
                "source_sha256": source_sha,
                "tables": len(report["counts"]),
                "rows": total_rows,
                "ledger": report["ledger"],
                "dump_sha256": hashlib.sha256(
                    dump.encode("utf-8")).hexdigest(),
                "next_step": "check-dump, then psql load on the target "
                             "(runbook §9); re-verify on the target before switching",
            }
            print(canonical(summary))
            return 0
        if args.cmd == "check-dump":
            with open(args.dump, "r", encoding="utf-8") as fh:
                result = check_dump(conn, fh.read())
            print(canonical(result))
            return 0 if result["ok"] else 1
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
