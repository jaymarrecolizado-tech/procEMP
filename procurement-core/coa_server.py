"""Step 8: read-only COA verification endpoint + offline CLI.

The HTTP surface is strictly GET-only and read-only: it recomputes hashes and
returns reports, and never writes to the ledger. Bind to loopback by default;
put an authenticated reverse proxy in front if COA needs LAN access.

Authenticated access (Next — persistence/ops): `serve --auth-token TOKEN`
(or COA_AUTH_TOKEN env). When set, every route except /health requires the
token via `X-Auth-Token` header or `?token=`/`?auth_token=` query param
(constant-time compare). The offline CLI (`verify-*`, `seal`, `export`)
never needs a token — it is a local file operation.

Optional TLS: `serve --tls-cert cert.pem --tls-key key.pem` (plus
`--tls-client-ca ca.pem` for mutual TLS). Transport protection does not
replace the token or human gates; the endpoint stays read-only.

  python coa_server.py serve --db procurement.db --port 8090
  python coa_server.py serve --db procurement.db --port 8090 --auth-token TOKEN
  python coa_server.py verify-chain --db procurement.db
  python coa_server.py verify-folder --db procurement.db --seal-id <id>
  python coa_server.py verify-corpus --db procurement.db
  python coa_server.py list-seals --db procurement.db
  python coa_server.py export --db procurement.db --seal-id <id> --out ./coa-package
  python coa_server.py seal --db procurement.db --tx-id <id> --sealed-by <name>
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from core import CoreError, _connect, verify_chain
from ops_tls import (
    add_security_headers, add_tls_arguments, build_tls_context, scheme_for,
    wrap_server,
)
from rag import verify_corpus_integrity
from audit import (
    compile_folder, export_package, list_seals, seal_folder,
    verify_sealed_folder,
)


def _assert_loopback(host):
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise CoreError(f"refusing to bind COA endpoint to non-loopback host: {host}")


def _authed(headers, params, auth_token):
    """True when the request carries a matching token (or auth is disabled)."""
    if auth_token is None:
        return True
    for key in ("token", "auth_token"):
        provided = params.get(key) or ""
        if provided and hmac.compare_digest(provided, auth_token):
            return True
    provided = (headers or {}).get("X-Auth-Token", "") or ""
    return bool(provided) and hmac.compare_digest(provided, auth_token)


def handle(conn, target, auth_token=None, headers=None):
    """Pure request handler — testable without opening a socket."""
    parsed = urllib.parse.urlparse(target)
    path = parsed.path.rstrip("/") or "/"
    params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    if path == "/health":
        return 200, {"status": "ok", "service": "procurement-coa-verification",
                     "mode": "read-only"}
    if not _authed(headers, params, auth_token):
        return 401, {"error": "unauthorized",
                     "hint": "provide X-Auth-Token header or ?token= (COA_AUTH_TOKEN)"}
    if path == "/verify-chain":
        try:
            from_seq = int(params.get("from", 1))
            to_seq = int(params["to"]) if "to" in params else None
        except ValueError:
            return 400, {"error": "from/to must be integers"}
        return 200, verify_chain(conn, from_seq, to_seq)
    if path == "/verify-folder":
        if "seal_id" not in params:
            return 400, {"error": "seal_id is required"}
        return 200, verify_sealed_folder(conn, params["seal_id"])
    if path == "/folder":
        if "seal_id" not in params:
            return 400, {"error": "seal_id is required"}
        row = conn.execute("SELECT content_json FROM sealed_folder WHERE id=?",
                           (params["seal_id"],)).fetchone()
        if not row:
            return 404, {"error": "unknown seal_id"}
        return 200, json.loads(row["content_json"])
    if path == "/integrity":
        return 200, verify_corpus_integrity(conn)
    if path == "/list-seals":
        return 200, {"seals": list_seals(conn, params.get("tx_id"))}
    return 404, {"error": "unknown endpoint",
                 "available": ["/health", "/verify-chain", "/verify-folder",
                               "/folder", "/integrity", "/list-seals"]}


def make_handler(conn, *, auth_token=None, tls=False):
    class COAHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                status, payload = handle(conn, self.path, auth_token=auth_token,
                                         headers=self.headers)
            except Exception as exc:
                status, payload = 500, {"error": str(exc)}
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            add_security_headers(self.send_header, tls=tls)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.dumps({"error": "read-only endpoint"}).encode("utf-8")
            self.send_response(405)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            add_security_headers(self.send_header, tls=tls)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            sys.stderr.write("[coa] " + (fmt % args) + "\n")

    return COAHandler


def serve(conn, host="127.0.0.1", port=8090, *, auth_token=None,
          tls_cert=None, tls_key=None, tls_client_ca=None):
    _assert_loopback(host)
    tls_context = build_tls_context(tls_cert, tls_key, tls_client_ca)
    server = HTTPServer((host, port),
                        make_handler(conn, auth_token=auth_token,
                                     tls=tls_context is not None))
    wrap_server(server, tls_context)
    scheme = scheme_for(tls_context)
    print(f"COA read-only verification endpoint on {scheme}://{host}:{port}")
    print("Routes: /health /verify-chain /verify-folder /folder /integrity /list-seals")
    if tls_context is not None:
        print("TLS ENABLED (TLS 1.2+)"
              + ("; mutual TLS required (client certificate)." if tls_client_ca else "."))
    if auth_token:
        print("Shared-secret auth ENABLED (X-Auth-Token or ?token= required except /health).")
    else:
        print("WARNING: no auth token configured; loopback-only read-only prototype.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="COA verification endpoint / CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="start the read-only HTTP endpoint")
    p.add_argument("--db", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--auth-token", default=os.environ.get("COA_AUTH_TOKEN"),
                   help="Shared secret; falls back to COA_AUTH_TOKEN env")
    add_tls_arguments(p, env_prefix="COA")

    p = sub.add_parser("verify-chain")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="from_seq", type=int, default=1)
    p.add_argument("--to", dest="to_seq", type=int, default=None)

    p = sub.add_parser("verify-folder")
    p.add_argument("--db", required=True)
    p.add_argument("--seal-id", required=True)

    p = sub.add_parser("verify-corpus")
    p.add_argument("--db", required=True)

    p = sub.add_parser("list-seals")
    p.add_argument("--db", required=True)
    p.add_argument("--tx-id", default=None)

    p = sub.add_parser("export")
    p.add_argument("--db", required=True)
    p.add_argument("--seal-id", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("seal", help="admin: create a sealed COA folder")
    p.add_argument("--db", required=True)
    p.add_argument("--tx-id", required=True)
    p.add_argument("--sealed-by", required=True)
    p.add_argument("--supersede-reason", default=None)

    p = sub.add_parser("compile", help="admin: compile without sealing")
    p.add_argument("--db", required=True)
    p.add_argument("--tx-id", required=True)

    args = parser.parse_args(argv)
    conn = _connect(args.db)
    try:
        if args.command == "serve":
            serve(conn, args.host, args.port, auth_token=args.auth_token,
                  tls_cert=args.tls_cert, tls_key=args.tls_key,
                  tls_client_ca=args.tls_client_ca)
            return 0
        if args.command == "verify-chain":
            result = verify_chain(conn, args.from_seq, args.to_seq)
        elif args.command == "verify-folder":
            result = verify_sealed_folder(conn, args.seal_id)
        elif args.command == "verify-corpus":
            result = verify_corpus_integrity(conn)
        elif args.command == "list-seals":
            result = {"seals": list_seals(conn, args.tx_id)}
        elif args.command == "export":
            result = export_package(conn, args.seal_id, args.out)
        elif args.command == "seal":
            result = seal_folder(conn, args.tx_id, args.sealed_by,
                                 args.supersede_reason)
        elif args.command == "compile":
            result = compile_folder(conn, args.tx_id)
        else:
            result = {"error": "unknown command"}
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
