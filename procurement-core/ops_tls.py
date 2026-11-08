"""Optional TLS + browser security headers for the loopback prototype services.

The prototype binds loopback only, so TLS is defense-in-depth (e.g. host-to-host
access behind an authenticated reverse proxy, or a workstation behind a local
gateway). Standard library only — the ``ssl`` module; no certificate generation
and no key material is ever written by this module.

Operator-supplied files:
  --tls-cert       PEM certificate chain (full chain recommended)
  --tls-key        PEM private key (keep out of the repo and backups)
  --tls-client-ca  optional CA bundle; when set, mutual TLS is REQUIRED
                   (clients must present a certificate signed by this CA)

Env fallbacks: ``PROCEMP_TLS_CERT`` / ``PROCEMP_TLS_KEY`` / ``PROCEMP_TLS_CLIENT_CA``
(review UI uses ``REVIEW_*``, COA uses ``COA_*`` via ``add_tls_arguments``).

TLS never replaces human gates or application auth: it protects transport only.
"""

from __future__ import annotations

import os
import ssl

# Applies to every response. The review UI uses one inline <style> block and no
# scripts, so the CSP stays strict and script-free.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
     "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
)
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000")


def add_security_headers(send_header, tls=False):
    """Add defensive response headers via the caller's ``send_header``."""
    for name, value in SECURITY_HEADERS:
        send_header(name, value)
    if tls:
        send_header(*HSTS_HEADER)


def add_tls_arguments(parser, env_prefix="PROCEMP"):
    """Attach TLS flags to a ``serve`` subparser/parser with env fallbacks."""
    parser.add_argument(
        "--tls-cert", default=os.environ.get(f"{env_prefix}_TLS_CERT"),
        help=f"PEM certificate chain; enables HTTPS (env {env_prefix}_TLS_CERT)")
    parser.add_argument(
        "--tls-key", default=os.environ.get(f"{env_prefix}_TLS_KEY"),
        help=f"PEM private key; never commit (env {env_prefix}_TLS_KEY)")
    parser.add_argument(
        "--tls-client-ca", default=os.environ.get(f"{env_prefix}_TLS_CLIENT_CA"),
        help=("CA bundle; when set, mutual TLS is required "
              f"(env {env_prefix}_TLS_CLIENT_CA)"))
    return parser


def build_tls_context(cert_file=None, key_file=None, client_ca=None):
    """Return an ``SSLContext`` (TLS 1.2+) or ``None`` when TLS is not configured.

    Raises ``ValueError`` for half-configured TLS so a service never silently
    starts in plaintext when the operator intended HTTPS.
    """
    if client_ca and not (cert_file and key_file):
        raise ValueError("--tls-client-ca requires --tls-cert and --tls-key")
    if not cert_file and not key_file:
        return None
    if not cert_file or not key_file:
        raise ValueError("both --tls-cert and --tls-key are required to enable TLS")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    if client_ca:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=client_ca)
    return context


def scheme_for(tls_context):
    return "https" if tls_context is not None else "http"


def wrap_server(server, tls_context):
    """Wrap a live server's listening socket; returns the server."""
    if tls_context is None:
        return server
    server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    return server