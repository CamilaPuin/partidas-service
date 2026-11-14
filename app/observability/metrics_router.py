import hmac
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.observability.http_metrics import REGISTRY


router = APIRouter()
basic_auth = HTTPBasic(auto_error=False, realm="metrics")


def metrics_credentials() -> tuple[str, str]:
    username = os.getenv("METRICS_SCRAPE_USERNAME") or "metrics"
    password = os.getenv("METRICS_SCRAPE_PASSWORD")
    if not username.strip() or password is None or not password.strip():
        raise RuntimeError("Las credenciales de scraping deben estar configuradas")
    return username, password


def require_metrics_auth(
    credentials: HTTPBasicCredentials | None = Depends(basic_auth),
) -> None:
    expected_username, expected_password = metrics_credentials()
    valid = credentials is not None
    if valid:
        valid = hmac.compare_digest(
            credentials.username.encode("utf-8"), expected_username.encode("utf-8")
        ) and hmac.compare_digest(
            credentials.password.encode("utf-8"), expected_password.encode("utf-8")
        )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Credenciales de métricas inválidas",
            headers={"WWW-Authenticate": 'Basic realm="metrics"'},
        )


@router.get("/metrics", include_in_schema=False, dependencies=[Depends(require_metrics_auth)])
def metrics() -> Response:
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
