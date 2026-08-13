import base64
import hashlib
import http.server
import json
import os
import threading
import urllib.parse

import pytest

from sicqg_triad.mcp_auth import MCPOAuthClient, TokenSet, generate_pkce


class MockOAuthHandler(http.server.BaseHTTPRequestHandler):
    """Mock RFC 9728 / 8414 / 7591 / token endpoints."""

    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if self.path == "/.well-known/oauth-protected-resource":
            self._send_json({
                "resource": base,
                "authorization_servers": [base],
            })
        elif self.path == "/.well-known/oauth-authorization-server":
            self._send_json({
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
            })
        elif self.path.startswith("/authorize"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            assert qs["code_challenge_method"] == ["S256"]
            assert qs["resource"] == [self.server.expected_resource]
            self.server.captured_challenge = qs["code_challenge"][0]
            redirect = qs["redirect_uri"][0]
            loc = f"{redirect}?code=mock-code-123&state={qs['state'][0]}"
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length)
        if self.path == "/register":
            data = json.loads(raw)
            self._send_json({
                "client_id": "mock-client-1",
                "redirect_uris": data["redirect_uris"],
            })
        elif self.path == "/token":
            form = urllib.parse.parse_qs(raw.decode())
            verifier = form["code_verifier"][0]
            # server-side PKCE check
            expect = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).rstrip(b"=").decode()
            assert expect == self.server.captured_challenge
            assert form["resource"] == [self.server.expected_resource]
            self._send_json({
                "access_token": "SECRET-ACCESS-TOKEN-xyz",
                "refresh_token": "SECRET-REFRESH-TOKEN-xyz",
                "expires_in": 3600,
                "scope": "mcp:read",
            })
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def mock_server(tmp_path):
    srv = http.server.HTTPServer(("127.0.0.1", 0), MockOAuthHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


def test_pkce_challenge_is_b64url_sha256_no_padding():
    verifier, challenge = generate_pkce()
    assert len(verifier) == 128
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expect
    assert "=" not in challenge


def test_full_pkce_flow_against_local_mock(mock_server, tmp_path):
    base = f"http://127.0.0.1:{mock_server.server_address[1]}"
    mock_server.expected_resource = base
    before = set(os.listdir(tmp_path))
    os.chdir(tmp_path)
    try:
        client = MCPOAuthClient(base, redirect_port=0)
        md = client.discover()
        assert md["token_endpoint"].endswith("/token")
        reg = client.register({"token_endpoint_auth_method": "none"})
        assert reg["client_id"] == "mock-client-1"
        tokens = client.authorize(scope="mcp:read", resource=base)
        assert tokens.access_token == "SECRET-ACCESS-TOKEN-xyz"
        assert tokens.scope == "mcp:read"
    finally:
        os.chdir("/")
    # module created no files anywhere in cwd
    assert set(os.listdir(tmp_path)) == before


def test_tokenset_repr_redacts():
    ts = TokenSet(
        access_token="SECRET-ACCESS-TOKEN-xyz",
        refresh_token="SECRET-REFRESH-TOKEN-xyz",
        expires_at=123.0,
        scope="mcp:read",
    )
    r = repr(ts)
    assert "SECRET-ACCESS-TOKEN-xyz" not in r
    assert "SECRET-REFRESH-TOKEN-xyz" not in r
    assert "redacted" in r


def test_refresh_requires_token(mock_server):
    client = MCPOAuthClient("http://127.0.0.1:1")
    ts = TokenSet("a", None, 0.0, "s")
    with pytest.raises(RuntimeError, match="refresh"):
        client.refresh(ts)
