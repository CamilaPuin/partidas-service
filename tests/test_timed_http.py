import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.errors import (
    CaseNotFound,
    CasesServiceError,
    IncorrectSolution,
    MatchLocked,
    MatchNotFound,
    MatchUnavailable,
    NoEligibleCase,
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


def headers():
    return {
        "X-Gateway-Secret": "test-only-gateway-secret",
        "X-User-Id": "user-1",
    }


def test_timed_create_route_keeps_existing_response_contract(client, monkeypatch):
    monkeypatch.setattr(
        matches.create_timed_match_use_case,
        "execute",
        lambda case_id, seconds, user_id: {
            "matchId": "match-1",
            "caseId": case_id,
            "startedAt": 100,
            "deadline": 100 + seconds,
        },
    )

    response = client.post(
        "/matches/timed",
        headers=headers(),
        json={"caseId": "case-1", "timeLimitSeconds": 60},
    )

    assert response.status_code == 200
    assert response.json() == {
        "matchId": "match-1",
        "caseId": "case-1",
        "startedAt": 100,
        "deadline": 160,
        "status": None,
        "player1": None,
        "player2": None,
    }


def test_timed_create_route_rejects_locked_case(client, monkeypatch):
    def fail(*_args):
        raise MatchLocked

    monkeypatch.setattr(matches.create_timed_match_use_case, "execute", fail)

    response = client.post(
        "/matches/timed",
        headers=headers(),
        json={"caseId": "case-1", "timeLimitSeconds": 60},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "El nivel actual no desbloquea este caso"}


def test_versus_create_route_does_not_accept_or_choose_case_from_client(client, monkeypatch):
    monkeypatch.setattr(
        matches.match_service,
        "create_versus_match",
        lambda user_id: {
            "matchId": "match-1",
            "caseId": None,
            "status": "WAITING",
            "player1": user_id,
        },
    )

    response = client.post(
        "/matches/versus",
        headers=headers(),
        json={},
    )

    assert response.status_code == 200
    assert response.json()["caseId"] is None
    assert response.json()["status"] == "WAITING"


def test_versus_create_route_reports_no_eligible_case(client, monkeypatch):
    def fail(*_args):
        raise NoEligibleCase

    monkeypatch.setattr(matches.match_service, "create_versus_match", fail)

    response = client.post("/matches/versus", headers=headers(), json={})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "No hay casos disponibles que puedas jugar y no hayas resuelto"
    }


def test_timed_finish_route_keeps_result_response_contract(client, monkeypatch):
    monkeypatch.setattr(
        matches.finish_timed_match_use_case,
        "execute",
        lambda *_args: {"won": True, "timeSeconds": 12, "bonus": 8, "xpDelta": 58},
    )

    response = client.post(
        "/matches/timed/match-1/finish",
        headers=headers(),
        json={"positions": [{"characterId": "c1", "x": 0, "y": 0}]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "won": True,
        "timeSeconds": 12,
        "bonus": 8,
        "xpDelta": 58,
        "sagaStatus": "PENDING",
    }


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (MatchNotFound(), 404, "Partida no encontrada"),
        (IncorrectSolution(), 400, "Solución incorrecta"),
        (OwnMatch(), 403, "No perteneces a esta partida"),
        (MatchUnavailable(), 400, "La partida ya terminó"),
        (CasesServiceError("cases down"), 503, "cases down"),
        (CaseNotFound("missing case"), 404, "missing case"),
        (ServiceIntegrationError("users down"), 502, "users down"),
    ],
)
def test_timed_finish_route_preserves_exception_status_and_body(
    client,
    monkeypatch,
    error,
    status,
    detail,
):
    def fail(*_args):
        raise error

    monkeypatch.setattr(matches.finish_timed_match_use_case, "execute", fail)

    response = client.post(
        "/matches/timed/match-1/finish",
        headers=headers(),
        json={"positions": [{"characterId": "c1", "x": 0, "y": 0}]},
    )

    assert response.status_code == status
    assert response.json() == {"detail": detail}
