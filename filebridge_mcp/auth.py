"""Optional bearer-token auth for HTTP mode — a single shared secret, not OAuth.

Clients must send ``Authorization: Bearer <token>`` matching the configured
secret; anything else gets 401. This is the pragmatic way to gate a self-hosted
HTTP MCP endpoint without standing up an authorization server. It applies to the
HTTP transport only — stdio carries no headers and is inherently local/single-client.
"""

from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings


class StaticTokenVerifier(TokenVerifier):
    """Accept exactly one shared-secret bearer token (constant-time compare)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="filebridge", scopes=[])
        return None


def auth_settings(host: str, port: int) -> AuthSettings:
    """Minimal AuthSettings whose OAuth-metadata URLs point at this server.

    A bind-all host (0.0.0.0 / ::) is not a connectable address, so the advertised
    URL falls back to localhost; static-token clients only need the bearer header.
    """
    url_host = "localhost" if host in ("0.0.0.0", "::") else host
    base = f"http://{url_host}:{port}"
    return AuthSettings(issuer_url=base, resource_server_url=base)
