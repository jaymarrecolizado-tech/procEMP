# ProcEMP VPS deployment kit (CloudPanel / nginx)

Deploys the two ProcEMP services behind an authenticated, TLS-terminating
reverse proxy. The app itself stays **loopback-only** (by design — both
servers refuse non-loopback binds); nginx provides HTTPS for browsers.

```
Browser ──HTTPS(Let's Encrypt)──▶ CloudPanel nginx
                                    ├─ review.<domain> ──▶ 127.0.0.1:8091  (review_server.py, systemd)
                                    └─ coa.<domain>    ──▶ 127.0.0.1:8090  (coa_server.py serve, systemd)
```

Subdomains (not path prefixes) are used because the app's pages use absolute
paths (`/draft`, `/item`), which break under prefix rewriting.

## Layout on the VPS

| Path | Purpose |
|---|---|
| `/opt/procemp/app/` | Application code (this repo; `procurement-core/` inside) |
| `/opt/procemp/data/` | `procurement.db`, `drafts/`, backups — the only writable dir |
| `/opt/procemp/etc/procemp.env` | Secrets (auth tokens), `chmod 600` |
| `/etc/systemd/system/procemp-*.service` | Service units |

## Steps

1. **Provision** (Debian 12 / Ubuntu 22.04+ — what CloudPanel requires):
   ```bash
   sudo apt-get update && sudo apt-get install -y python3 rsync
   ```
   Python needs to be **3.10+** (Debian 12 ships 3.11 ✓). No pip packages
   are needed — ProcEMP is stdlib-only.

2. **Copy this repo to the VPS** (from your workstation):
   ```bash
   rsync -av --exclude .git --exclude __pycache__ --exclude drafts \
     ./ deploy@<vps>:/tmp/procemp-src/
   ```
   Then on the VPS run: `sudo bash /tmp/procemp-src/deploy/deploy.sh /tmp/procemp-src`

3. **DNS**: create two A records pointing at the VPS:
   `review.<your-domain>` and `coa.<your-domain>`.

4. **CloudPanel**: create two sites (type can be "Static"), for
   `review.<domain>` and `coa.<domain>`, with Let's Encrypt certificates.
   Then edit each site's vhost and paste the matching file from
   `deploy/nginx/` (replaces the default `location /` block).

5. **Verify**:
   ```bash
   systemctl status procemp-review procemp-coa
   curl -s http://127.0.0.1:8091/health
   # from outside (with the token):
   curl -s https://review.<domain>/health
   curl -s -H "X-Auth-Token: <token>" https://review.<domain>/queue | head
   # full rehearsal:
   cd /opt/procemp/app/procurement-core && python3 ops_smoke.py \
     --db /opt/procemp/data/procurement.db \
     --review-url http://127.0.0.1:8091/health \
     --coa-url http://127.0.0.1:8090/health
   ```

## Security checklist (do not skip)

- [ ] `REVIEW_AUTH_TOKEN` / `COA_AUTH_TOKEN` set to long random values
      (`openssl rand -hex 32`); without them the servers run open on
      loopback and the proxy becomes the only gate
- [ ] Firewall: only 22/80/443 open — **never** 8090/8091
- [ ] SSH keys only, root login disabled, fail2ban on (CloudPanel base has it)
- [ ] Office-IP allowlist in the vhosts (uncomment the `allow/deny` lines),
      or basic auth at the proxy for defense-in-depth
- [ ] `/opt/procemp/etc/procemp.env` is `chmod 600`, owned by `procemp`
- [ ] Nightly `ops_backup.py` + off-machine copy (cron example in the unit pack)
- [ ] **Data governance:** real bid/procurement data on a public VPS is a
      personal-data-processing decision — DPO/DPIA sign-off required
      (`procurement-review/05-data-governance-training.md`). Until then,
      keep real transactions on the workstation; use the VPS for demos and
      pilots with fictional data only.
