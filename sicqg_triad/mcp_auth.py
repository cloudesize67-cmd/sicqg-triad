"""OAuth 2.1 client for MCP servers.

Implements:
  - RFC 9728: OAuth 2.0 Protected Resource Metadata discovery
  - RFC 8414: Authorization Server Metadata discovery
  - RFC 7591: Dynamic Client Registration
  - RFC 7636: PKCE (S256)
  - RFC 8707: Resource Indicators
Loopback redirect listener per RFC 8252 section 7.3.

SECURITY: tokens live in memory only. This module performs NO file I/O and
never logs token values; TokenSet.__repr__ redacts all secrets.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import secrets as _secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass

import requests


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge). Verifier is 128 chars; challenge is
    BASE64URL(SHA256(verifier)) without padding (RFC 7636 S256)."""
    verifier = _b64url(_secrets.token_bytes(96))  # 96 bytes -> 128 chars
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float
    scope: str

    def __repr__(self) -> str:
        rt = "<redacted>" if self.refresh_token else "None"
        return (
            f"TokenSet(access_token=<redacted>, refresh_token={rt}, "
            f"expires_at={self.expires_at:.0f}, scope={self.scope!r})"
        )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serves the loopback redirect exactly once."""

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        self.server.callback_params = {k: v[0] for k, v in qs.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authorization received; you may close this tab.")
        self.server.got_callback.set()

    def log_message(self, format, *args):  # silence request logging
        pass


class MCPOAuthClient:
    def __init__(self, resource_url: str, redirect_port: int = 0) -> None:
        self.resource_url = resource_url.rstrip("/")
        self.redirect_port = redirect_port
        # single-use state nonce, regenerated per authorize() call
        self._state: str | None = None
        self._metadata: dict | None = None
        self._as_metadata: dict | None = None
        self._client: dict | None = None

    # -- RFC 9728 + RFC 8414 discovery -------------------------------------
    def discover(self) -> dict:
        """Fetch protected-resource metadata (RFC 9728) then the
        authorization-server metadata (RFC 8414). Returns the AS metadata."""
        pr_url = f"{self.resource_url}/.well-known/oauth-protected-resource"
        resp = requests.get(pr_url, timeout=15)
        resp.raise_for_status()
        self._metadata = resp.json()
        auth_servers = self._metadata.get("authorization_servers") or []
        if not auth_servers:
            raise RuntimeError(f"no authorization_servers in {pr_url}")
        as_url = auth_servers[0].rstrip("/")
        resp = requests.get(
            f"{as_url}/.well-known/oauth-authorization-server", timeout=15
        )
        resp.raise_for_status()
        self._as_metadata = resp.json()
        return self._as_metadata

    # -- RFC 7591 dynamic client registration ------------------------------
    def register(self, metadata: dict) -> dict:
        """Register dynamically; returns the registration response including
        ``client_id``. Calls discover() first if needed."""
        if self._as_metadata is None:
            self.discover()
        reg_endpoint = self._as_metadata["registration_endpoint"]
        redirect_uri = self._redirect_uri()
        body = {"redirect_uris": [redirect_uri], **metadata}
        resp = requests.post(reg_endpoint, json=body, timeout=15)
        resp.raise_for_status()
        self._client = resp.json()
        return self._client

    def _redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}/callback"

    # -- Authorization code + PKCE (RFC 7636) + resource (RFC 8707) ---------
    def authorize(self, scope: str, resource: str) -> TokenSet:
        """Full authorization-code flow with PKCE S256.

        Spins up a loopback listener (on redirect_port, or an ephemeral port
        if 0), builds the authorization request with ``resource`` per
        RFC 8707, and exchanges the returned code at the token endpoint.
        State is a 32-byte nonce, single-use.
        """
        if self._as_metadata is None:
            self.discover()
        if self._client is None:
            self.register({"token_endpoint_auth_method": "none"})

        listener = http.server.HTTPServer(("127.0.0.1", self.redirect_port),
                                          _CallbackHandler)
        listener.got_callback = threading.Event()
        listener.callback_params = None
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        try:
            port = listener.server_address[1]
            redirect_uri = f"http://127.0.0.1:{port}/callback"

            verifier, challenge = generate_pkce()
            self._state = _b64url(_secrets.token_bytes(32))  # 32-byte nonce

            params = {
                "response_type": "code",
                "client_id": self._client["client_id"],
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": self._state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,  # RFC 8707
            }
            auth_url = (
                self._as_metadata["authorization_endpoint"]
                + "?"
                + urllib.parse.urlencode(params)
            )
            # In an interactive client this opens a browser; here we follow
            # the redirect programmatically (no browser dependency).
            resp = requests.get(auth_url, timeout=15, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                cb_url = resp.headers["Location"]
                requests.get(cb_url, timeout=15)  # deliver to loopback listener
            else:
                resp.raise_for_status()

            if not listener.got_callback.wait(timeout=30):
                raise TimeoutError("no authorization callback received")
            cb = listener.callback_params or {}
            state = self._state
            self._state = None  # single-use: consumed now
            if cb.get("state") != state:
                raise RuntimeError("state mismatch (possible CSRF)")
            if "error" in cb:
                raise RuntimeError(f"authorization error: {cb['error']}")
            code = cb["code"]

            return self._exchange(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self._client["client_id"],
                    "code_verifier": verifier,
                    "resource": resource,  # RFC 8707
                },
                scope=scope,
            )
        finally:
            listener.shutdown()
            listener.server_close()
            thread.join(timeout=5)

    def _exchange(self, form: dict, scope: str) -> TokenSet:
        resp = requests.post(
            self._as_metadata["token_endpoint"], data=form, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + float(data.get("expires_in", 3600)),
            scope=data.get("scope", scope),
        )

    def refresh(self, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise RuntimeError("no refresh token available")
        return self._exchange(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": self._client["client_id"],
                "resource": self.resource_url,  # RFC 8707
            },
            scope=tokens.scope,
        )
