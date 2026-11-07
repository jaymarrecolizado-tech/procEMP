# Deployment & Operations Runbook (loopback prototype → VPS)

> **Advisory decision-support system.** AI flags/drafts/recommendations never
> finalize statutory procurement decisions; named humans decide and sign.

## 1. Services (all loopback-only)

| Service | Started with | Port | Binds |
|---|---|---|---|
| Review UI | `python review_server.py --db procurement.db [--auth-token TOKEN]` | 8091 | 127.0.0.1 only |
| COA verification (read-only) | `python coa_server.py serve --db procurement.db` | 8090 | 127.0.0.1 only |
| Extraction stub (tests) | `python training_adapter.py serve-stub --port 8080` | 8080 | 127.0.0.1 only |
| Extraction adapter | `python model_adapter_server.py --upstream-kind ollama/openai/llama_cpp --upstream-url http://127.0.0.1:... --model <name>` | 8092 | 127.0.0.1 only |
| Model plumbing check | `python model_plumbing_check.py check --url http://127.0.0.1:8092/extract` | — | loopback URL only |

Non-loopback binds are refused in code. For LAN/VPS access, put an
authenticated reverse proxy in front (proxy auth + TLS) — do not expose
these ports directly.

## 2. Review queue operations

```bash
cd procurement-core
python review_queue.py list --status OPEN
python review_queue.py dashboard
python review_queue.py export --out review-queue.html
```

Role-aware actions (enforced via `review_queue.ROLE_PERMISSIONS`):
claim/recommend/resolve (BAC roles), override → correction (BAC_STAFF /
SECRETARIAT / TWG), adjudicate (SECOND_REVIEWER / CHAIR / SECRETARIAT;
submitter can never self-adjudicate).

## 3. Extraction / model readiness

```bash
python dataset.py provenance          # synthetic-only confirmation
python dataset.py sft --out sft.jsonl
python evaluation.py stub --json      # dry-run with stub backend
python readiness_report.py --format json           # blockers (fail closed)
python readiness_report.py --format md --out readiness.md --db procurement.db
```

Real fine-tuning requires approved/anonymized samples + DPO/BAC sign-off —
synthetic sets are plumbing/format validation only.

## 4. Human-gated training pipeline (no auto-deploy)

```bash
# 1. batch + dataset + CANDIDATE (needs >=3 adjudicated corrections)
python training_job.py plan --db procurement.db --created-by ml-01 --version v2
# 2. operator runs QLoRA SFT externally in the approved environment
# 3. record weights hash + frozen eval (approval still separate)
python training_job.py register-eval --db procurement.db --version v2 \
  --weights-sha256 <64-hex> --evaluator evaluator-01
```

`approve_candidate` / `deploy_candidate` / `promote_canary` are separate
named-human steps via `learning.py`. Rollback restores the prior version.

## 5. Backup / restore drills / retention

```bash
python ops_backup.py backup --db procurement.db --out backups/db-<date>.db
python ops_backup.py verify  --backup backups/db-<date>.db
python ops_backup.py restore-drill --backup backups/db-<date>.db --restore-to /tmp/restore.db
python ops_backup.py retention --db procurement.db
```

Drills verify the hash chain + corpus integrity on a COPY; they never touch
the live database. Sealed folders + ledger are retained indefinitely (WORM).

## 6. Local → VPS migration

```python
from core import migrate
migrate(source_conn, "procurement.db")  # preserves seq/hashes, verifies chain
```

## 7. Checklists (mode coverage)

```bash
python checklist_builder.py --tx-id <id> --approved-by bac-secretariat-01 --abc 1000000
```

Templates exist for Competitive Bidding + SVP (RA12009 only). Unsupported
modes and unverified regimes (legacy RA9184) fail closed — the BAC defines
checklists per transaction instead.
