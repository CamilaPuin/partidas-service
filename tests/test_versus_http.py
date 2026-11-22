import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.errors import (
    CaseNotFound,
    CasesServiceError,
    IncorrectSolution,
    ServiceIntegrationError,
)
from app.core.gateway_security import GatewaySecretMiddleware
from app.routers import matches


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "test-only-gateway-secret")
    app = FastAPI()
    app.add_middleware(GatewaySecretMiddleware)
    app.include_router(matches.router)
    return TestClient(app)


HEADERS = {
    "X-Gateway-Secret": "test-only-gateway-secret",
    "X-User-Id": "user-1",
}


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (IncorrectSolution(), 400, "Solución incorrecta"),
        (CaseNotFound("missing"), 404, "missing"),
        (CasesServiceError("cases down"), 503, "cases down"),
        (ServiceIntegrationError("users down"), 502, "users down"),
    ],
)
def test_versus_submission_preserves_domain_and_upstream_error_mapping(
    client,
    monkeypatch,
    error,
    status,
    detail,
):
    def fail(*_args):
        raise error

    monkeypatch.setattr(matches.match_service, "submit_versus_solution", fail)
    response = client.post(
        "/matches/versus/match-1/submit",
        headers=HEADERS,
        json={"positions": [{"characterId": "S1", "x": 0, "y": 0}]},
    )

    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_versus_submission_rejects_duplicate_character_ids_before_use_case(
    client,
    monkeypatch,
):
    called = []
    monkeypatch.setattr(
        matches.match_service,
        "submit_versus_solution",
        lambda *_args: called.append(True),
    )

    response = client.post(
        "/matches/versus/match-1/submit",
        headers=HEADERS,
        json={
            "positions": [
                {"characterId": "S1", "x": 0, "y": 0},
                {"characterId": "S1", "x": 1, "y": 0},
            ]
        },
    )

    assert response.status_code == 422
    assert called == []


def test_versus_creator_can_cancel_waiting_room(client, monkeypatch):
    monkeypatch.setattr(
        matches.match_service,
        "cancel_versus_match",
        lambda match_id, user_id: {
            "matchId": match_id,
            "status": "CANCELLED",
        },
    )

    response = client.delete("/matches/versus/match-1", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"matchId": "match-1", "status": "CANCELLED"}


def test_versus_progress_websocket_streams_authorized_match_snapshots(client, monkeypatch):
    snapshot = {
        "caseId": "case-1",
        "status": "IN_PROGRESS",
        "me": 0.2,
        "rival": 0.6,
        "opponentName": "Rival",
    }

    class FakePubSub:
        def __init__(self):
            self.notifications = 1

        def subscribe(self, _channel):
            pass

        def get_message(self, **_kwargs):
            if self.notifications:
                self.notifications -= 1
                return {"type": "message", "data": "progress"}
            raise RuntimeError("stop fake stream after one published update")

        def unsubscribe(self, _channel):
            pass

        def close(self):
            pass

    monkeypatch.setattr(matches.match_service, "get_versus_progress", lambda *_args: snapshot)
    monkeypatch.setattr(matches.redis_client, "pubsub", FakePubSub)

    with client.websocket_connect(
        "/matches/versus/match-1/progress/ws",
        headers=HEADERS,
    ) as websocket:
        initial = websocket.receive_json()
        update = websocket.receive_json()

    assert initial == {"event": "snapshot", "progress": snapshot}
    assert update == {"event": "progress", "progress": snapshot}
