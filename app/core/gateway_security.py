import hashlib
import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def configured_gateway_secret() -> str:
    secret = os.getenv("GATEWAY_SECRET")
    if secret is None or not secret.strip():
        raise RuntimeError("GATEWAY_SECRET no está configurado")
    return secret


def has_valid_gateway_secret(received: str | None, expected: str) -> bool:
    if received is None:
        return False
    received_digest = hashlib.sha256(received.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(received_digest, expected_digest)


def is_health_request(method: str, path: str) -> bool:
    return method == "GET" and path == "/health"


class GatewaySecretMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.expected_secret = configured_gateway_secret()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if is_health_request(request.method, request.url.path):
            return await call_next(request)

        received = request.headers.get("X-Gateway-Secret")
        if not has_valid_gateway_secret(received, self.expected_secret):
            return JSONResponse(
                status_code=403,
                content={"detail": "X-Gateway-Secret inválido o ausente"},
            )

        return await call_next(request)
