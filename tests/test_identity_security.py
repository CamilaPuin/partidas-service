import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.core.gateway_security import (
    GatewaySecretMiddleware,
    configured_gateway_secret,
    has_valid_gateway_secret,
)
from app.routers.matches import get_current_user_id
from app.schemas.matches import TimedMatchRequest


class FakeRequest:
    def __init__(self, method, path, headers=None):
        self.method = method
        self.url = SimpleNamespace(path=path)
        self.headers = headers or {}


def dispatch_gateway_middleware(monkeypatch, method, path, headers=None):
    monkeypatch.setenv("GATEWAY_SECRET", "gateway-test-secret")
    middleware = GatewaySecretMiddleware(lambda *_args: None)
    called = {"next": False}

    async def call_next(_request):
        called["next"] = True
        return Response(status_code=204)

    response = asyncio.run(
        middleware.dispatch(FakeRequest(method, path, headers), call_next)
    )
    return response, called["next"]


def test_external_id_dependency_accepts_keycloak_subject():
    subject = "603fba8d-bb2c-4994-bc27-e9c40e30fec8"
    assert get_current_user_id(subject) == subject


@pytest.mark.parametrize("subject", [None, "", "   ", "user/name", "bad_id", "x" * 65])
def test_external_id_dependency_rejects_invalid_values_without_echoing(subject):
    with pytest.raises(HTTPException) as error:
        get_current_user_id(subject)
    assert error.value.status_code == 403
    assert error.value.detail == "El encabezado X-User-Id es inválido"
    if subject:
        assert subject not in str(error.value.detail)


def test_gateway_secret_comparison_accepts_only_exact_secret():
    assert has_valid_gateway_secret("gateway-test-secret", "gateway-test-secret")
    assert not has_valid_gateway_secret("gateway-test-secreU", "gateway-test-secret")
    assert not has_valid_gateway_secret(None, "gateway-test-secret")


def test_gateway_secret_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("GATEWAY_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="GATEWAY_SECRET"):
        configured_gateway_secret()


def test_health_keeps_only_get_secret_free(monkeypatch):
    response, called = dispatch_gateway_middleware(monkeypatch, "GET", "/health")
    assert response.status_code == 204
    assert called

    response, called = dispatch_gateway_middleware(monkeypatch, "HEAD", "/health")
    assert response.status_code == 403
    assert not called


def test_only_get_metrics_is_exempt_for_its_own_basic_auth(monkeypatch):
    response, called = dispatch_gateway_middleware(monkeypatch, "GET", "/metrics")
    assert response.status_code == 204
    assert called

    response, called = dispatch_gateway_middleware(monkeypatch, "HEAD", "/metrics")
    assert response.status_code == 403
    assert not called

    response, called = dispatch_gateway_middleware(
        monkeypatch,
        "GET",
        "/matches/ranking",
        {"Authorization": "Basic dGVzdDpwYXNzd29yZA=="},
    )
    assert response.status_code == 403
    assert not called


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_requires_gateway_secret(monkeypatch, path):
    response, called = dispatch_gateway_middleware(monkeypatch, "GET", path)
    assert response.status_code == 403
    assert not called


def test_gateway_secret_allows_protected_request(monkeypatch):
    response, called = dispatch_gateway_middleware(
        monkeypatch,
        "GET",
        "/matches/ranking",
        {"X-Gateway-Secret": "gateway-test-secret"},
    )
    assert response.status_code == 204
    assert called


def test_timed_duration_is_bounded():
    assert TimedMatchRequest(caseId="case-1", timeLimitSeconds=60)
    assert TimedMatchRequest(caseId="case-1", timeLimitSeconds=3600)
    with pytest.raises(ValueError):
        TimedMatchRequest(caseId="case-1", timeLimitSeconds=59)
    with pytest.raises(ValueError):
        TimedMatchRequest(caseId="case-1", timeLimitSeconds=3601)
