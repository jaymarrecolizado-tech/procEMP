# ProcEMP E2E tests (Playwright — dev-only)

Browser-level tests for the drafting form (`/draft`). The application
itself remains **stdlib-only**; Playwright is a development-only dependency
and is never imported by the app or by `procurement-core/tests/`.

## Setup (once)

```bash
pip install playwright
python -m playwright install chromium
```

## Run

```bash
python e2e/test_draft_scenarios.py
```

The harness starts its **own** review server on an ephemeral port with a
**temp database** — it never touches `procurement.db` or any running
services. Scenario coverage:

| # | Scenario | Expected |
|---|----------|----------|
| 1 | Full chain from defaults (3 suppliers + award) | 6 watermarked PDFs, `RO2-PR-` ledger numbering |
| 2 | Suppliers cleared (pre-canvass) | PR + RFQ only — award docs never drafted |
| 3 | Award to the **highest** bidder | rendering refused, violation shown, no results |
| 4 | Required field cleared | browser blocks submission, operator stays on form |
| 5 | Download link | real `%PDF` bytes, `Content-Disposition: attachment` |
| 6 | Token auth (separate server) | no/wrong token → 401; `?token=` → form |
