from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from app.core.error_responses import register_error_handlers


def make_client():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/missing")
    def missing():
        raise HTTPException(status_code=404, detail="Caso no encontrado")

    @app.get("/upstream")
    def upstream():
        raise HTTPException(status_code=503, detail="raw internal dependency message")

    @app.get("/validation")
    def validation(limit: int = Query(ge=1)):
        return {"limit": limit}

    @app.get("/unexpected")
    def unexpected():
        raise RuntimeError("database password must not escape")

    return TestClient(app, raise_server_exceptions=False)


def test_http_errors_use_shared_envelope_and_safe_request_id():
    response = make_client().get("/missing", headers={"X-Request-Id": "req-123"})
    assert response.status_code == 404
    assert response.json() == {
        "status": 404,
        "error": "NOT_FOUND",
        "message": "Caso no encontrado",
        "requestId": "req-123",
    }


def test_upstream_and_unexpected_errors_do_not_expose_internal_messages():
    client = make_client()
    upstream = client.get("/upstream")
    unexpected = client.get("/unexpected")
    assert upstream.json() == {
        "status": 503,
        "error": "UPSTREAM_UNAVAILABLE",
        "message": "Un servicio requerido no está disponible",
    }
    assert unexpected.json() == {
        "status": 500,
        "error": "INTERNAL_ERROR",
        "message": "Error interno del servicio",
    }


def test_request_validation_uses_422_and_machine_readable_details():
    response = make_client().get("/validation?limit=0")
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "VALIDATION_ERROR"
    assert payload["status"] == 422
    assert payload["details"]["errors"]
    assert "'input':" not in str(payload).lower()
