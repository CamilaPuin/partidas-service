import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.errors import (
    CaseNotFound,
    CasesServiceError,
    MatchConflict,
    MatchNotFound,
    MatchUnavailable,
    OwnMatch,
    ServiceIntegrationError,
)
from app.core.gateway_security import GatewaySecretMiddleware
from app.routers import matches


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "test-only-gateway-secret")
    test_app = FastAPI()
    test_app.add_middleware(GatewaySecretMiddleware)
    test_app.include_router(matches.router)
    return TestClient(test_app)


HEADERS = {
    "X-Gateway-Secret": "test-only-gateway-secret",
    "X-User-Id": "user-1",
}


def raise_error(error):
    def fail(*_args):
        raise error

    return fail


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (CaseNotFound("missing"), 404, "missing"),
        (CasesServiceError("cases down"), 503, "cases down"),
        (ServiceIntegrationError("registry down"), 502, "registry down"),
    ],
)
def test_classic_create_keeps_external_error_mapping(
    client,
    monkeypatch,
    error,
    status,
    detail,
):
    monkeypatch.setattr(
        matches.match_service,
        "create_classic_match",
        raise_error(error),
    )

    response = client.post(
        "/matches/classic",
        headers=HEADERS,
        json={"caseId": "case-1"},
    )

    assert response.status_code == status
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (MatchNotFound(), 404, "Partida no encontrada"),
        (OwnMatch(), 403, "No perteneces a la partida"),
        (
            MatchConflict(),
            409,
            "El intento contradice un intento previo o la partida está finalizando",
        ),
        (MatchUnavailable(), 409, "La partida ya no está disponible"),
        (CaseNotFound("missing"), 404, "missing"),
        (CasesServiceError("cases down"), 503, "cases down"),
        (ServiceIntegrationError("users down"), 502, "users down"),
    ],
)
def test_classic_attempt_keeps_error_status_and_body(
    client,
    monkeypatch,
    error,
    status,
    detail,
):
    monkeypatch.setattr(
        matches.match_service,
        "attempt_classic_match",
        raise_error(error),
    )

    response = client.post(
        "/matches/classic/match-1/attempts",
        headers=HEADERS,
        json={
            "attemptId": "ac5ef82c-2747-4bfc-bccd-fd6e0d12d03f",
            "positions": [{"characterId": "c1", "x": 0, "y": 0}],
        },
    )

    assert response.status_code == status
    assert response.json() == {"detail": detail}
