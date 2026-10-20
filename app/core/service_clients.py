import json
import os
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ServiceIntegrationError(Exception):
    pass


def _eureka_registry_url() -> str:
    eureka_server = os.getenv("EUREKA_SERVER")
    if not eureka_server:
        raise ServiceIntegrationError("EUREKA_SERVER no está configurado")

    base_url = eureka_server.rstrip("/")
    if not base_url.endswith("/eureka"):
        base_url = f"{base_url}/eureka"
    return f"{base_url}/apps"


def _service_url(service_name: str) -> str:
    registry_url = f"{_eureka_registry_url()}/{quote(service_name, safe='')}"
    request = Request(registry_url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            registry = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ServiceIntegrationError(
            f"No se pudo consultar Eureka para {service_name}"
        ) from exc

    if not isinstance(registry, dict):
        raise ServiceIntegrationError(f"Eureka devolvió un registro inválido para {service_name}")
    application = registry.get("application") or {}
    if not isinstance(application, dict):
        raise ServiceIntegrationError(f"Eureka devolvió un registro inválido para {service_name}")
    instances = application.get("instance") or []
    if isinstance(instances, dict):
        instances = [instances]
    if not isinstance(instances, list):
        raise ServiceIntegrationError(f"Eureka devolvió un registro inválido para {service_name}")

    for instance in instances:
        if not isinstance(instance, dict):
            continue
        if instance.get("status") != "UP":
            continue

        secure_port = instance.get("securePort") or {}
        if secure_port.get("@enabled") in ("true", True):
            port = secure_port.get("$")
            protocol = "https"
        else:
            port = (instance.get("port") or {}).get("$")
            protocol = "http"

        host = instance.get("hostName")
        if host and port:
            return f"{protocol}://{host}:{port}"

    raise ServiceIntegrationError(
        f"No hay instancias disponibles de {service_name} en Eureka"
    )


def post_service_json(
    service_name: str,
    path: str,
    payload: Dict[str, Any],
    expect_json: bool = False,
) -> Any:
    service_url = _service_url(service_name)
    request = Request(
        f"{service_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ServiceIntegrationError(
            f"{service_name} rechazó o no pudo procesar la solicitud"
        ) from exc

    if not body or not expect_json:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ServiceIntegrationError(
            f"{service_name} devolvió una respuesta JSON inválida"
        ) from exc


def validate_case(case_id: str, positions: list[int]) -> bool:
    response = post_service_json(
        "CASOS-SERVICE",
        f"/cases/{quote(case_id, safe='')}/validate",
        {"positions": positions},
        expect_json=True,
    )
    if isinstance(response, bool):
        return response
    if isinstance(response, dict):
        for result_key in ("isCorrect", "correct", "valid", "isValid"):
            if isinstance(response.get(result_key), bool):
                return response[result_key]
    raise ServiceIntegrationError(
        "CASOS-SERVICE devolvió una respuesta de validación inesperada"
    )


def register_user_result(
    user_id: int,
    case_id: str,
    mode: str,
    won: bool,
    time_seconds: int,
    xp_delta: int,
) -> None:
    post_service_json(
        "USUARIOS-SERVICE",
        f"/users/{user_id}/results",
        {
            "caseId": case_id,
            "mode": mode,
            "won": won,
            "timeSeconds": time_seconds,
            "xpDelta": xp_delta,
        },
    )
