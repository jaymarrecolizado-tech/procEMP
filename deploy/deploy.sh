#!/usr/bin/env bash
# ProcEMP VPS installer — run ON THE VPS as root/sudo with the repo path:
#   sudo bash deploy/deploy.sh /tmp/procemp-src
# Idempotent: safe to re-run (never overwrites data/ or secrets).
set -euo pipefail

SRC="${1:?usage: deploy.sh <repo-root>}"
APP_DIR=/opt/procemp/app
DATA_DIR=/opt/procemp/data
ETC_DIR=/opt/procemp/etc
ENV_FILE=$ETC_DIR/procemp.env

[ -d "$SRC/procurement-core" ] || { echo "repo root looks wrong: $SRC"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not installed"; exit 1; }
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || { echo "python3 >= 3.10 required, found $PYV"; exit 1; }

echo "==> creating procemp user/dirs"
id procemp >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin procemp
mkdir -p "$APP_DIR" "$DATA_DIR/drafts" "$ETC_DIR"

echo "==> copying application (rsync, excludes .git/pycache/drafts/db)"
rsync -a --exclude .git --exclude __pycache__ --exclude drafts \
      --exclude '*.db' --exclude sample-dashboard.html \
      "$SRC/" "$APP_DIR/"
chown -R procemp:procemp "$APP_DIR" "$DATA_DIR"

echo "==> secrets"
if [ ! -f "$ENV_FILE" ]; then
  {
    echo "REVIEW_AUTH_TOKEN=$(openssl rand -hex 32)"
    echo "COA_AUTH_TOKEN=$(openssl rand -hex 32)"
  } > "$ENV_FILE"
  echo "    generated new tokens in $ENV_FILE"
else
  echo "    kept existing $ENV_FILE"
fi
chown procemp:procemp "$ENV_FILE" && chmod 600 "$ENV_FILE"

echo "==> database (init only if absent)"
if [ ! -f "$DATA_DIR/procurement.db" ]; then
  sudo -u procemp python3 -c \
    "import sys;sys.path.insert(0,'$APP_DIR/procurement-core');from core import init_db;c=init_db('$DATA_DIR/procurement.db');c.close()"
  echo "    created $DATA_DIR/procurement.db (schema + seeds)"
else
  echo "    kept existing $DATA_DIR/procurement.db"
fi

echo "==> systemd units"
cp "$SRC/deploy/procemp-review.service" "$SRC/deploy/procemp-coa.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now procemp-review.service procemp-coa.service

echo "==> status"
systemctl --no-pager --lines=3 status procemp-review procemp-coa || true
sleep 1
curl -fs http://127.0.0.1:8091/health >/dev/null && echo "review  :8091 healthy"
curl -fs http://127.0.0.1:8090/health >/dev/null && echo "coa     :8090 healthy"
echo
echo "Tokens (for browser login and COA calls):"
grep -E '^REVIEW_AUTH_TOKEN' "$ENV_FILE" | cut -c1-30 && echo "  ...(full value in $ENV_FILE)"
echo
echo "Next: DNS A-records -> this VPS, CloudPanel sites + Let's Encrypt,"
echo "then paste deploy/nginx/*.conf into each site vhost (see deploy/README.md)."
