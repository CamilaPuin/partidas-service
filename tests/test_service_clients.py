import json

from app.core import service_clients


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"valid":true,"killerFound":true,"correctCount":4,"total":4}'


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
        return FakeResponse()

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    service_clients.register_user_result(
        external_id, "case-1", "TIMED", True, 42, 50
    )

    request = seen["request"]
    assert request.full_url.endswith(f"/users/{external_id}/results")
    assert request.get_header("X-internal-token") == "internal-test"
    assert json.loads(request.data) == {
        "caseId": "case-1",
        "mode": "TIMED",
        "won": True,
        "timeSeconds": 42,
        "xpDelta": 50,
    }


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
