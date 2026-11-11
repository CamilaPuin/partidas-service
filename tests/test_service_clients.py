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
    assert service_clients.validate_case("case-1", [{"characterId": "V1", "x": 0, "y": 0}]) is True
    assert seen["request"].get_header("X-gateway-secret") == "secret-test"


def test_validate_case_sends_positions_objects(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "secret-test")
    monkeypatch.setattr(service_clients, "_service_url", lambda _name: "http://casos-service:8082")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["payload"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    positions = [{"characterId": "V1", "x": 0, "y": 0}, {"characterId": "S1", "x": 1, "y": 0}]
    assert service_clients.validate_case("case-1", positions) is True
    assert seen["payload"] == {"positions": positions}


def test_register_user_result_preserves_opaque_user_id(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal-test")
    monkeypatch.setattr(service_clients, "_service_url", lambda _name: "http://usuarios-service:8081")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["token"] = request.get_header("X-internal-token")
        return FakeResponse()

    monkeypatch.setattr(service_clients, "urlopen", fake_urlopen)
    service_clients.register_user_result(
        "11111111-2222-4333-8444-555555555555",
        "case-1",
        "TIMED",
        True,
        12,
        50,
    )

    assert seen["url"] == (
        "http://usuarios-service:8081/users/"
        "11111111-2222-4333-8444-555555555555/results"
    )
    assert seen["token"] == "internal-test"
