"""Bearer-token authentication as plain ASGI middleware.

Runs before routing and before any database access. The token is compared in
constant time and is never logged or echoed back.
"""

import hmac

from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import error_response


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: bytes) -> None:
        self.app = app
        self._token = token

    def _authorized(self, scope: Scope) -> bool:
        header = None
        for name, value in scope["headers"]:
            if name == b"authorization":
                header = value
                break
        if header is None:
            return False
        scheme, _, credentials = header.partition(b" ")
        # The auth scheme is case-insensitive (RFC 7235); the token is not.
        if scheme.lower() != b"bearer" or not credentials:
            return False
        return hmac.compare_digest(credentials, self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        response = error_response(401, "unauthorized", "missing or invalid bearer token")
        response.headers["WWW-Authenticate"] = "Bearer"
        await response(scope, receive, send)
