from fastapi import APIRouter, Header, HTTPException, Query
from typing import List

from app.core.redis_client import redis_client
from app.core.service_clients import ServiceIntegrationError
from app.repositories.matches import MatchRepository
from app.schemas.matches import (
    MatchDTO,
    MatchResultDTO,
    ProgressRequest,
    RankingEntryDTO,
    ResultDTO,
    SolutionDTO,
    TimedMatchRequest,
    VersusMatchRequest,
    VersusResultDTO,
)
from app.services.matches import (
    IncorrectSolution,
    MatchNotFound,
    MatchService,
    MatchUnavailable,
    OwnMatch,
)

router = APIRouter()
match_service = MatchService(MatchRepository(redis_client))


@router.post(
    "/matches/timed",
    response_model=MatchDTO,
    status_code=200,
    summary="Crear partida contrarreloj",
    responses={
        200: {"description": "Partida contrarreloj creada"},
        422: {"description": "Solicitud inválida"},
    },
)
def create_timed_match(
    request: TimedMatchRequest,
    user_id: int = Header(..., alias="X-User-Id"),
):
    return match_service.create_timed_match(request, user_id)


@router.post(
    "/matches/timed/{match_id}/finish",
    response_model=ResultDTO,
    status_code=200,
    summary="Finalizar partida contrarreloj",
    responses={
        400: {"description": "La solución es incorrecta o la partida ya terminó"},
        403: {"description": "El usuario no pertenece a la partida"},
        404: {"description": "Partida no encontrada"},
        200: {"description": "Resultado final de la partida"},
        422: {"description": "Solicitud inválida"},
        502: {"description": "Error al validar o registrar el resultado"},
    },
)
def finish_timed_match(
    match_id: str,
    solution: SolutionDTO,
    user_id: int = Header(..., alias="X-User-Id"),
):
    try:
        return match_service.finish_timed_match(match_id, user_id, solution)
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except IncorrectSolution as exc:
        raise HTTPException(status_code=400, detail="Solución incorrecta") from exc
    except OwnMatch as exc:
        raise HTTPException(status_code=403, detail="No perteneces a esta partida") from exc
    except MatchUnavailable as exc:
        raise HTTPException(status_code=400, detail="La partida ya terminó") from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/matches/versus",
    response_model=MatchDTO,
    status_code=200,
    summary="Crear partida versus",
    responses={
        200: {"description": "Partida versus creada"},
        422: {"description": "Solicitud inválida"},
    },
)
def create_versus_match(
    request: VersusMatchRequest,
    user_id: int = Header(..., alias="X-User-Id"),
):
    return match_service.create_versus_match(request, user_id)


@router.get(
    "/matches/versus/open",
    response_model=List[MatchDTO],
    status_code=200,
    summary="Listar partidas versus disponibles",
    responses={200: {"description": "Partidas disponibles"}},
)
def list_open_matches():
    return match_service.list_open_matches()


@router.post(
    "/matches/versus/{match_id}/join",
    response_model=MatchDTO,
    status_code=200,
    summary="Unirse a una partida versus",
    responses={
        400: {"description": "La partida no está disponible"},
        404: {"description": "Partida no encontrada"},
        200: {"description": "Partida unida correctamente"},
        422: {"description": "Solicitud inválida"},
    },
)
def join_versus_match(
    match_id: str,
    user_id: int = Header(..., alias="X-User-Id"),
):
    try:
        return match_service.join_versus_match(match_id, user_id)
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except MatchUnavailable as exc:
        raise HTTPException(
            status_code=400,
            detail="La partida ya no está disponible",
        ) from exc
    except OwnMatch as exc:
        raise HTTPException(
            status_code=400,
            detail="No puedes unirte a tu propia partida",
        ) from exc


@router.put(
    "/matches/versus/{match_id}/progress",
    status_code=200,
    summary="Actualizar progreso de la partida",
    responses={
        200: {"description": "Progreso registrado"},
        422: {"description": "Solicitud inválida"},
    },
)
def report_progress(
    match_id: str,
    request: ProgressRequest,
    user_id: int = Header(..., alias="X-User-Id"),
):
    return match_service.report_progress(match_id, user_id, request)


@router.get(
    "/matches/versus/{match_id}/progress",
    status_code=200,
    summary="Consultar progreso de la partida",
    responses={
        200: {"description": "Progreso de los dos participantes"},
        422: {"description": "Parámetros inválidos"},
    },
)
def get_versus_progress(
    match_id: str,
    rival_id: int = Query(..., alias="rivalId"),
    user_id: int = Header(..., alias="X-User-Id"),
):
    return match_service.get_versus_progress(
        match_id,
        user_id,
        rival_id,
    )


@router.post(
    "/matches/versus/{match_id}/submit",
    response_model=VersusResultDTO,
    status_code=200,
    summary="Enviar solución de partida versus",
    responses={
        400: {"description": "La partida no está en curso o la solución es incorrecta"},
        403: {"description": "El usuario no pertenece a la partida"},
        404: {"description": "Partida no encontrada"},
        200: {"description": "Ganador de la partida"},
        422: {"description": "Solicitud inválida"},
        502: {"description": "Error al validar o registrar el resultado"},
    },
)
def submit_versus_solution(
    match_id: str,
    solution: SolutionDTO,
    user_id: int = Header(..., alias="X-User-Id"),
):
    try:
        return match_service.submit_versus_solution(match_id, user_id, solution)
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except IncorrectSolution as exc:
        raise HTTPException(status_code=400, detail="Solución incorrecta") from exc
    except OwnMatch as exc:
        raise HTTPException(status_code=403, detail="No perteneces a esta partida") from exc
    except MatchUnavailable as exc:
        raise HTTPException(status_code=400, detail="La partida no está en curso") from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/matches/ranking",
    response_model=List[RankingEntryDTO],
    status_code=200,
    summary="Consultar ranking global",
    responses={
        200: {"description": "Ranking ordenado por puntuación"},
        422: {"description": "Límite inválido"},
    },
)
def get_ranking(limit: int = Query(10)):
    return match_service.get_ranking(limit)


@router.get(
    "/matches/history/me",
    response_model=List[MatchResultDTO],
    status_code=200,
    summary="Consultar historial del usuario actual",
    responses={
        200: {"description": "Historial de partidas del usuario"},
        422: {"description": "Encabezado X-User-Id inválido"},
    },
)
def get_my_history(user_id: int = Header(..., alias="X-User-Id")):
    return match_service.get_user_history(user_id)
