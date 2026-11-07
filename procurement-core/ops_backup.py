"""Operations: SQLite backup + verification + retention (Next — persistence).

Stdlib-only backup/restore drills toward production targets, WITHOUT adding
a PostgreSQL dependency. The system remains SQLite (stdlib); this module
provides the operational controls the plan calls for:

- backup: consistent SQLite backup + manifest (size, sha256, chain head)
- verify: chain + corpus integrity check on a backup file (read-only)
- restore-drill: restore to a temp file and verify (never overwrites source)
- retention: schedule helper that lists sealed folders / events by age to
  support the agency records-retention schedule (read-only, advisory)

PostgreSQL/WORM migration stays an external target; migrate() in core.py
already preserves the hash chain for local→VPS moves. Run drills on the
approved environment and record results in the ledger/runbook.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3

from core import _connect, canonical, now, sha256_text, verify_chain
from rag import verify_corpus_integrity

RETENTION_POLICY = {
    "version": "1.0-draft",
    "status": "DRAFT — confirm with COA/DPO/legal before production",
    "sealed_folders": "retain indefinitely (WORM); never delete",
    "ledger_events": "retain indefinitely (append-only audit trail)",
    "procurement_db_backups": "7 daily + 12 monthly; WAL/journal excluded",
    "training_jsonl": "retain with dataset provenance; no confidential content",
    "review_html_exports": "treat as derived views; source of truth is the DB",
}


def backup_db(db_path, out_path, *, actor="ops"):
    """Consistent backup via SQLite backup API + manifest."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    source = _connect(db_path)
    try:
        chain = verify_chain(source)
        corpus = verify_corpus_integrity(source)
    finally:
        source.close()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    # SQLite backup API for consistency (no concurrent-write corruption).
    source = _connect(db_path)
    backup = _connect(out_path)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    size = os.path.getsize(out_path)
    with open(out_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    manifest = {
        "backed_up_at": now(),
        "source": os.path.basename(db_path),
        "backup": os.path.basename(out_path),
        "bytes": size,
        "sha256": digest,
        "chain_ok": chain["ok"],
        "chain_head": chain["head_hash"],
        "corpus_ok": corpus["ok"],
        "actor": actor,
        "retention_policy": RETENTION_POLICY,
    }
    manifest_path = out_path + ".manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    manifest["manifest_path"] = manifest_path
    return manifest


def verify_backup(backup_path):
    """Read-only verification of a backup file."""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"backup not found: {backup_path}")
    conn = _connect(backup_path)
    try:
        chain = verify_chain(conn)
        corpus = verify_corpus_integrity(conn)
        seals = conn.execute("SELECT count(*) c FROM sealed_folder").fetchone()["c"]
        events = conn.execute("SELECT count(*) c FROM ledger_event").fetchone()["c"]
    finally:
        conn.close()
    return {
        "checked_at": now(),
        "backup": os.path.basename(backup_path),
        "chain_ok": chain["ok"],
        "chain_head": chain["head_hash"],
        "events_checked": chain["events_checked"],
        "events_total": events,
        "corpus_ok": corpus["ok"],
        "sealed_folders": seals,
        "ok": chain["ok"] and corpus["ok"],
    }


def restore_drill(backup_path, restore_path):
    """Restore a COPY to restore_path (never overwrites the backup) and verify.

    The drill proves backup → restore → verify end-to-end (approval evidence
    for the runbook). It does NOT touch the live database.
    """
    if os.path.abspath(backup_path) == os.path.abspath(restore_path):
        raise ValueError("restore_path must differ from backup_path")
    shutil.copyfile(backup_path, restore_path)
    report = verify_backup(restore_path)
    report["restored_to"] = restore_path
    report["drill"] = "restore-verify completed; live DB untouched"
    return report


def retention_report(db_path):
    """Advisory retention inventory (read-only)."""
    conn = _connect(db_path)
    try:
        seals = conn.execute(
            "SELECT tx_id, version, sealed_at FROM sealed_folder ORDER BY sealed_at"
        ).fetchall()
        events_by_year = [
            dict(r) for r in conn.execute(
                "SELECT substr(occurred_at,1,4) AS year, count(*) c FROM ledger_event "
                "GROUP BY 1 ORDER BY 1"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "checked_at": now(),
        "policy": RETENTION_POLICY,
        "sealed_folders": len(seals),
        "sealed_folders_by_year": {},
        "ledger_events_by_year": events_by_year,
        "note": "Advisory inventory only. Deletion/retention decisions need COA/DPO approval.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backup / verify / restore-drill / retention (advisory)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("backup")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--backup", required=True)
    p = sub.add_parser("restore-drill")
    p.add_argument("--backup", required=True)
    p.add_argument("--restore-to", required=True)
    p = sub.add_parser("retention")
    p.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "backup":
        print(json.dumps(backup_db(args.db, args.out), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "verify":
        print(json.dumps(verify_backup(args.backup), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "restore-drill":
        print(json.dumps(restore_drill(args.backup, args.restore_to), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "retention":
        print(json.dumps(retention_report(args.db), indent=2, ensure_ascii=False))
        return 0
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
