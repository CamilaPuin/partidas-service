import json
import os
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.application.errors import (
    CaseNotFound,
    CasesServiceError,
    ServiceIntegrationError,
    UserResultConflict,
)


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
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
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
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    service_name_upper = service_name.upper()
    if service_name_upper == "CASOS-SERVICE":
        gateway_secret = os.getenv("GATEWAY_SECRET")
        if not gateway_secret:
            raise CasesServiceError("GATEWAY_SECRET no está configurado")
        headers["X-Gateway-Secret"] = gateway_secret
    if service_name_upper == "USUARIOS-SERVICE":
        internal_token = os.getenv("INTERNAL_API_TOKEN")
        if not internal_token:
            raise ServiceIntegrationError("INTERNAL_API_TOKEN no está configurado")
        headers["X-Internal-Token"] = internal_token
    request = Request(
        f"{service_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=_http_timeout_seconds()) as response:
            body = response.read()
    except HTTPError as exc:
        if service_name_upper == "CASOS-SERVICE" and exc.code == 404:
            raise CaseNotFound("El caso solicitado no existe") from exc
        if service_name_upper == "USUARIOS-SERVICE" and exc.code == 409:
            raise UserResultConflict(
                "USUARIOS-SERVICE rechazó un resultado contradictorio"
            ) from exc
        raise ServiceIntegrationError(
            f"{service_name} rechazó o no pudo procesar la solicitud"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise ServiceIntegrationError(
            f"{service_name} rechazó o no pudo procesar la solicitud"
        ) from exc

    if not body or not expect_json:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ServiceIntegrationError(
            f"{service_name} devolvió una respuesta JSON inválida"
        ) from exc


def get_service_json(service_name: str, path: str) -> Any:
    service_url = _service_url(service_name)
    headers = {"Accept": "application/json"}
    if service_name.upper() == "CASOS-SERVICE":
        gateway_secret = os.getenv("GATEWAY_SECRET")
        if not gateway_secret:
            raise CasesServiceError("GATEWAY_SECRET no está configurado")
        headers["X-Gateway-Secret"] = gateway_secret

    request = Request(f"{service_url}{path}", headers=headers, method="GET")
    try:
        with urlopen(request, timeout=_http_timeout_seconds()) as response:
            body = response.read()
    except HTTPError as exc:
        if service_name.upper() == "CASOS-SERVICE" and exc.code == 404:
            raise CaseNotFound("El caso solicitado no existe") from exc
        raise ServiceIntegrationError(
            f"{service_name} rechazó o no pudo procesar la solicitud"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise ServiceIntegrationError(
            f"No se pudo consultar {service_name}"
        ) from exc

    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ServiceIntegrationError(
            f"{service_name} devolvió una respuesta JSON inválida"
        ) from exc


def _http_timeout_seconds() -> float:
    try:
        timeout = float(os.getenv("SERVICE_HTTP_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise ServiceIntegrationError(
            "SERVICE_HTTP_TIMEOUT_SECONDS debe ser un número positivo"
        ) from exc
    if timeout <= 0:
        raise ServiceIntegrationError(
            "SERVICE_HTTP_TIMEOUT_SECONDS debe ser un número positivo"
        )
    return timeout


def validate_case(case_id: str, positions: list[Any]) -> bool:
    try:
        response = post_service_json(
            "CASOS-SERVICE",
            f"/cases/{quote(case_id, safe='')}/validate",
            {
                "positions": [
                    position.model_dump() if hasattr(position, "model_dump") else position
                    for position in positions
                ]
            },
            expect_json=True,
        )
    except CaseNotFound:
        raise
    except ServiceIntegrationError as exc:
        raise CasesServiceError(f"CASOS-SERVICE no pudo validar la solución: {exc}") from exc
    if isinstance(response, dict):
        valid = response.get("valid")
        if isinstance(valid, bool):
            return valid
    raise CasesServiceError(
        "CASOS-SERVICE devolvió una respuesta de validación inesperada"
    )


def get_case_difficulty(case_id: str) -> str:
    try:
        response = get_service_json(
            "CASOS-SERVICE",
            f"/cases/{quote(case_id, safe='')}",
        )
    except CaseNotFound:
        raise
    except ServiceIntegrationError as exc:
        raise CasesServiceError(
            f"CASOS-SERVICE no pudo recuperar el caso: {exc}"
        ) from exc

    if not isinstance(response, dict):
        raise CasesServiceError("CASOS-SERVICE devolvió un caso inválido")
    difficulty = response.get("difficulty")
    if not isinstance(difficulty, str) or difficulty not in {"EASY", "MEDIUM", "HARD"}:
        raise CasesServiceError(
            "CASOS-SERVICE devolvió una dificultad no reconocida"
        )
    return difficulty


def register_user_result(
    user_id: str,
    match_id: str,
    case_id: str,
    mode: str,
    won: bool,
    time_seconds: int,
    xp_delta: int | None,
    difficulty: str | None = None,
) -> Dict[str, Any]:
    response = post_service_json(
        "USUARIOS-SERVICE",
        f"/users/{quote(user_id, safe='')}/results",
        {
            "matchId": match_id,
            "caseId": case_id,
            "mode": mode,
            "won": won,
            "timeSeconds": time_seconds,
            "difficulty": difficulty,
            "xpDelta": xp_delta,
        },
        expect_json=True,
    )
    if (
        not isinstance(response, dict)
        or response.get("matchId") != match_id
        or response.get("caseId") != case_id
        or response.get("mode") != mode
        or response.get("won") is not won
        or type(response.get("xpDelta")) is not int
        or not isinstance(response.get("firstWinRewarded"), bool)
        or (mode == "CLASSIC" and response["xpDelta"] < 0)
    ):
        raise ServiceIntegrationError(
            "USUARIOS-SERVICE devolvió un resultado incompatible"
        )
    return {
        "xpDelta": response["xpDelta"],
        "firstWinRewarded": response["firstWinRewarded"],
    }
