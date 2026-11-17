import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def error_code(status: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHENTICATED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "VALIDATION_ERROR",
        502: "UPSTREAM_ERROR",
        503: "UPSTREAM_UNAVAILABLE",
    }.get(status, "INTERNAL_ERROR" if status >= 500 else "REQUEST_ERROR")


def error_body(
    status: int,
    message: str,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": status,
        "error": error_code(status),
        "message": message[:300] or "Error de solicitud",
    }
    if request_id and _REQUEST_ID.fullmatch(request_id):
        body["requestId"] = request_id
    if details:
        body["details"] = details
    return body


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        status = exc.status_code
        if status == 502:
            message = "Error al consultar un servicio requerido"
        elif status == 503:
            message = "Un servicio requerido no está disponible"
        elif status >= 500:
            message = "Error interno del servicio"
        elif isinstance(exc.detail, str):
            message = exc.detail
        else:
            message = "La solicitud no es válida"
        return JSONResponse(
            status_code=status,
            content=error_body(status, message, request.headers.get("X-Request-Id")),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = []
        for item in exc.errors():
            location = [str(part) for part in item.get("loc", ()) if part != "body"]
            errors.append(
                {
                    "field": ".".join(location),
                    "message": str(item.get("msg", "Valor inválido"))[:200],
                }
            )
        return JSONResponse(
            status_code=422,
            content=error_body(
                422,
                "La solicitud contiene campos inválidos",
                request.headers.get("X-Request-Id"),
                {"errors": errors},
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=error_body(500, "Error interno del servicio", request.headers.get("X-Request-Id")),
        )
