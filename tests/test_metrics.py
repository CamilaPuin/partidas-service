import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from app import main
from app.core.gateway_security import GatewaySecretMiddleware
from app.observability.http_metrics import HttpRequestMetricsMiddleware
from app.observability.metrics_router import metrics_credentials
from app.observability.metrics_router import router as metrics_router
from app.routers import matches as matches_router


TEST_PASSWORD = "test-only-scrape-password"
TEST_TOKEN = "test-only-bearer-token"
TEST_MATCH_ID = "private-match-id-for-metrics-test"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "test-only-gateway-secret")
    monkeypatch.setenv("METRICS_SCRAPE_PASSWORD", TEST_PASSWORD)
    monkeypatch.delenv("METRICS_SCRAPE_USERNAME", raising=False)
    test_app = FastAPI()
    test_app.add_middleware(GatewaySecretMiddleware)
    test_app.add_middleware(HttpRequestMetricsMiddleware)
    test_app.include_router(matches_router.router)
    test_app.include_router(metrics_router)
    test_app.get("/health")(main.health_check)
    return TestClient(test_app, raise_server_exceptions=False)


def scrape(client):
    return client.get("/metrics", auth=("metrics", TEST_PASSWORD))


def test_metrics_requires_correct_basic_auth_and_sets_challenge(client):
    missing = client.get("/metrics")
    incorrect = client.get("/metrics", auth=("metrics", "incorrect-test-password"))
    correct = scrape(client)

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Basic")
    assert incorrect.status_code == 401
    assert incorrect.headers["www-authenticate"].startswith("Basic")
    assert correct.status_code == 200
    assert "text/plain" in correct.headers["content-type"]
    assert correct.headers["content-type"] == CONTENT_TYPE_LATEST


def test_metrics_password_is_mandatory(monkeypatch):
    monkeypatch.delenv("METRICS_SCRAPE_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="credenciales de scraping"):
        metrics_credentials()
    monkeypatch.setenv("METRICS_SCRAPE_PASSWORD", "   ")
    with pytest.raises(RuntimeError, match="credenciales de scraping"):
        metrics_credentials()


def test_metrics_username_can_be_configured(client, monkeypatch):
    monkeypatch.setenv("METRICS_SCRAPE_USERNAME", "custom-metrics-user")
    response = client.get("/metrics", auth=("custom-metrics-user", TEST_PASSWORD))
    assert response.status_code == 200


def test_basic_auth_does_not_bypass_gateway_security_for_match_routes(client):
    response = client.get("/matches/ranking", auth=("metrics", TEST_PASSWORD))
    assert response.status_code == 403


def test_health_keeps_its_public_behavior(client, monkeypatch):
    monkeypatch.setattr(main.redis_client, "ping", lambda: True)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_histogram_records_status_and_normalized_route_without_sensitive_labels(
    client, monkeypatch
):
    client.app.dependency_overrides[matches_router.get_current_user_id] = lambda: "test-user-id"
    monkeypatch.setattr(
        matches_router.match_service,
        "get_versus_progress",
        lambda match_id, user_id: {"matchId": match_id},
    )
    headers = {
        "X-Gateway-Secret": "test-only-gateway-secret",
        "X-User-Id": "test-user-id",
        "X-Roles": "USER",
        "Authorization": f"Bearer {TEST_TOKEN}",
    }

    try:
        response = client.get(
            f"/matches/versus/{TEST_MATCH_ID}/progress", headers=headers
        )
        not_found = client.get("/route-that-does-not-exist", headers=headers)
        assert response.status_code == 200
        assert not_found.status_code == 404

        metrics = scrape(client)
        assert metrics.status_code == 200
        assert 'application="partidas-service"' in metrics.text
        assert 'method="GET"' in metrics.text
        assert 'uri="/matches/versus/{match_id}/progress"' in metrics.text
        assert 'status="200"' in metrics.text
        assert 'uri="unmatched"' in metrics.text
        assert 'status="404"' in metrics.text
        assert "http_server_requests_seconds_bucket" in metrics.text
        assert "http_server_requests_seconds_count" in metrics.text
        assert "http_server_requests_seconds_sum" in metrics.text
        assert TEST_MATCH_ID not in metrics.text
        assert "test-user-id" not in metrics.text
        assert TEST_TOKEN not in metrics.text
        assert TEST_PASSWORD not in metrics.text
        assert 'uri="/metrics"' not in metrics.text
    finally:
        client.app.dependency_overrides.pop(matches_router.get_current_user_id, None)


def test_unhandled_route_exception_is_recorded_as_server_error(client, monkeypatch):
    client.app.dependency_overrides[matches_router.get_current_user_id] = lambda: "test-user-id"

    def fail_request(_match_id, _user_id):
        raise RuntimeError("synthetic test failure")

    monkeypatch.setattr(matches_router.match_service, "get_versus_progress", fail_request)
    headers = {"X-Gateway-Secret": "test-only-gateway-secret"}
    try:
        response = client.get(
            "/matches/versus/another-private-id/progress", headers=headers
        )
        assert response.status_code == 500
        metrics = scrape(client)
        assert 'uri="/matches/versus/{match_id}/progress"' in metrics.text
        assert 'status="500"' in metrics.text
        assert "another-private-id" not in metrics.text
        assert "synthetic test failure" not in metrics.text
    finally:
        client.app.dependency_overrides.pop(matches_router.get_current_user_id, None)
