import json
from urllib.error import HTTPError

from app.core import service_clients


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"valid":true,"killerFound":true,"correctCount":4,"total":4}'


class FakeResultResponse:
    def __init__(self, mode="TIMED", xp_delta=50):
        self.body = json.dumps(
            {
                "matchId": "match-1",
                "caseId": "case-1",
                "mode": mode,
                "won": True,
                "xpDelta": xp_delta,
                "firstWinRewarded": mode == "CLASSIC",
            }
        ).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_validate_case_sends_gateway_secret(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "secret-test")
    monkeypatch.setattr(service_clients, "_service_url", lambda _name: "http://casos-service:8082")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    assert service_clients.validate_case(
        "case-1", [{"characterId": "V1", "x": 0, "y": 0}]
    ) is True
    assert seen["request"].get_header("X-gateway-secret") == "secret-test"


def test_validate_case_sends_positions_objects(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "secret-test")
    monkeypatch.setattr(service_clients, "_service_url", lambda _name: "http://casos-service:8082")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["payload"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    positions = [
        {"characterId": "V1", "x": 0, "y": 0},
        {"characterId": "S1", "x": 1, "y": 0},
    ]
    assert service_clients.validate_case("case-1", positions) is True
    assert seen["payload"] == {"positions": positions}


def test_get_case_difficulty_uses_internal_cases_request(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "secret-test")
    monkeypatch.setattr(
        service_clients,
        "_service_url",
        lambda _name: "http://casos-service:8082",
    )
    seen = {}

    class CaseResponse(FakeResponse):
        def read(self):
            return b'{"id":"case-1","difficulty":"HARD"}'

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return CaseResponse()

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    assert service_clients.get_case_difficulty("case-1") == "HARD"
    assert seen["request"].full_url.endswith("/cases/case-1")
    assert seen["request"].get_header("X-gateway-secret") == "secret-test"


def test_register_user_result_uses_external_id_and_internal_token(monkeypatch):
    external_id = "603fba8d-bb2c-4994-bc27-e9c40e30fec8"
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal-test")
    monkeypatch.setattr(
        service_clients,
        "_service_url",
        lambda _name: "http://usuarios-service:8081",
    )
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return FakeResultResponse()

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    result = service_clients.register_user_result(
        external_id, "match-1", "case-1", "TIMED", True, 42, 50
    )

    request = seen["request"]
    assert request.full_url.endswith(f"/users/{external_id}/results")
    assert request.get_header("X-internal-token") == "internal-test"
    assert json.loads(request.data) == {
        "matchId": "match-1",
        "caseId": "case-1",
        "mode": "TIMED",
        "won": True,
        "timeSeconds": 42,
        "difficulty": None,
        "xpDelta": 50,
    }
    assert result == {"xpDelta": 50, "firstWinRewarded": False}


def test_register_classic_result_omits_client_calculated_xp(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal-test")
    monkeypatch.setattr(
        service_clients,
        "_service_url",
        lambda _name: "http://usuarios-service:8081",
    )
    seen = {}

    def fake_urlopen(request, timeout):
        seen["payload"] = json.loads(request.data)
        return FakeResultResponse(mode="CLASSIC", xp_delta=100)

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    result = service_clients.register_user_result(
        "external-1",
        "match-1",
        "case-1",
        "CLASSIC",
        True,
        42,
        None,
        "MEDIUM",
    )
    assert seen["payload"] == {
        "matchId": "match-1",
        "caseId": "case-1",
        "mode": "CLASSIC",
        "won": True,
        "timeSeconds": 42,
        "difficulty": "MEDIUM",
        "xpDelta": None,
    }
    assert result == {"xpDelta": 100, "firstWinRewarded": True}


def test_register_timed_result_preserves_negative_xp_delta(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal-test")
    monkeypatch.setattr(
        service_clients,
        "_service_url",
        lambda _name: "http://usuarios-service:8081",
    )
    monkeypatch.setattr(
        service_clients,
        "urlopen",
        lambda *_args, **_kwargs: FakeResultResponse(xp_delta=-10),
    )

    result = service_clients.register_user_result(
        "external-1",
        "match-1",
        "case-1",
        "TIMED",
        True,
        42,
        -10,
    )

    assert result == {"xpDelta": -10, "firstWinRewarded": False}


def test_user_result_conflict_is_exposed_as_conflict(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal-test")
    monkeypatch.setattr(
        service_clients,
        "_service_url",
        lambda _name: "http://usuarios-service:8081",
    )

    def conflict(request, timeout):
        raise HTTPError(request.full_url, 409, "conflict", {}, None)

    monkeypatch.setattr(service_clients, "urlopen", conflict)
    try:
        service_clients.register_user_result(
            "external-1",
            "match-1",
            "case-1",
            "CLASSIC",
            True,
            42,
            None,
            "MEDIUM",
        )
    except service_clients.UserResultConflict:
        pass
    else:
        raise AssertionError("El conflicto 409 debe conservarse")


def test_register_user_result_fails_closed_without_internal_token(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    monkeypatch.setattr(
        service_clients,
        "_service_url",
        lambda _name: "http://usuarios-service:8081",
    )

    try:
        service_clients.register_user_result(
            "603fba8d-bb2c-4994-bc27-e9c40e30fec8",
            "match-1",
            "case-1",
            "TIMED",
            True,
            42,
            50,
        )
    except service_clients.ServiceIntegrationError as exc:
        assert "INTERNAL_API_TOKEN" in str(exc)
    else:
        raise AssertionError("Missing internal token must fail closed")
