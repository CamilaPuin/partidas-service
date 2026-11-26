import asyncio
import os
from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket
from typing import List
import re

from app.application.errors import (
    CaseNotFound,
    CasesServiceError,
    IncorrectSolution,
    MatchConflict,
    MatchLocked,
    MatchmakingBusy,
    MatchNotFound,
    MatchUnavailable,
    NoEligibleCase,
    OwnMatch,
    ServiceIntegrationError,
)
from app.application.matches import MatchService
from app.application.timed_matches import CreateTimedMatch, FinishTimedMatch
from app.core.redis_client import redis_client
from app.core.gateway_security import has_valid_gateway_secret
from app.infrastructure.adapters.timed_match import (
    CasesHttpAdapter,
    SystemClock,
    UsersHttpAdapter,
    UuidMatchIdGenerator,
)
from app.repositories.matches import MatchRepository
from app.schemas.matches import (
    ClassicAttemptRequest,
    ClassicAttemptResultDTO,
    ClassicMatchRequest,
    MatchDTO,
    MatchResultDTO,
    ProgressRequest,
    RankingEntryDTO,
    ResultDTO,
    SagaStatusDTO,
    SolutionDTO,
    TimedMatchRequest,
    VersusMatchRequest,
    VersusMatchDTO,
    VersusResultDTO,
)

router = APIRouter(
    responses={
        403: {
            "description": "X-Gateway-Secret o X-User-Id ausente o inválido",
        }
    }
)
match_repository = MatchRepository(redis_client)
cases_adapter = CasesHttpAdapter()
users_adapter = UsersHttpAdapter()
clock_adapter = SystemClock()
ids_adapter = UuidMatchIdGenerator()
match_service = MatchService(
    store=match_repository,
    cases=cases_adapter,
    users=users_adapter,
    clock=clock_adapter,
    ids=ids_adapter,
)
create_timed_match_use_case = CreateTimedMatch(
    store=match_repository,
    cases=cases_adapter,
    users=users_adapter,
    clock=clock_adapter,
    ids=ids_adapter,
)
finish_timed_match_use_case = FinishTimedMatch(
    store=match_repository,
    cases=cases_adapter,
    users=users_adapter,
    clock=clock_adapter,
)
EXTERNAL_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}\Z")


def get_current_user_id(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str:
    if x_user_id is None or not EXTERNAL_ID_PATTERN.fullmatch(x_user_id):
        raise HTTPException(
            status_code=403,
            detail="El encabezado X-User-Id es inválido",
        )
    return x_user_id


@router.post(
    "/matches/classic",
    response_model=MatchDTO,
    status_code=200,
    summary="Crear partida clásica",
    responses={
        200: {"description": "Partida clásica creada"},
        403: {"description": "El nivel del usuario no desbloquea la dificultad"},
        404: {"description": "Caso no encontrado"},
        503: {"description": "Casos no disponible o dificultad inválida"},
    },
)
def create_classic_match(
    request: ClassicMatchRequest,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.create_classic_match(request.caseId, user_id)
    except CaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CasesServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MatchLocked as exc:
        raise HTTPException(
            status_code=403,
            detail="El nivel actual no desbloquea esta dificultad",
        ) from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/matches/classic/{match_id}/attempts",
    response_model=ClassicAttemptResultDTO,
    response_model_exclude_none=True,
    status_code=200,
    summary="Enviar intento de partida clásica",
    responses={
        200: {"description": "Resultado del intento; uno incorrecto deja la partida en curso"},
        403: {"description": "El usuario no pertenece a la partida"},
        404: {"description": "Partida o caso no encontrado"},
        409: {"description": "Intento contradictorio o partida no disponible"},
        502: {"description": "Error al registrar el resultado en usuarios"},
        503: {"description": "Casos no disponible o respuesta de validación inválida"},
    },
)
def attempt_classic_match(
    match_id: str,
    request: ClassicAttemptRequest,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.attempt_classic_match(
            match_id,
            user_id,
            str(request.attemptId),
            request.positions,
        )
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except OwnMatch as exc:
        raise HTTPException(status_code=403, detail="No perteneces a la partida") from exc
    except MatchConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="El intento contradice un intento previo o la partida está finalizando",
        ) from exc
    except MatchUnavailable as exc:
        raise HTTPException(status_code=409, detail="La partida ya no está disponible") from exc
    except CaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CasesServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/matches/timed",
    response_model=MatchDTO,
    status_code=200,
    summary="Crear partida contrarreloj",
    responses={
        200: {"description": "Partida contrarreloj creada"},
        403: {"description": "El nivel del usuario no desbloquea el caso"},
        404: {"description": "Caso no encontrado"},
        502: {"description": "Usuarios no disponible"},
        503: {"description": "Casos no disponible"},
        422: {"description": "Solicitud inválida"},
    },
)
def create_timed_match(
    request: TimedMatchRequest,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return create_timed_match_use_case.execute(
            request.caseId,
            request.timeLimitSeconds,
            user_id,
        )
    except MatchLocked as exc:
        raise HTTPException(
            status_code=403,
            detail="El nivel actual no desbloquea este caso",
        ) from exc
    except CaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CasesServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
        502: {"description": "Error al registrar el resultado en usuarios"},
        503: {"description": "Casos no disponible o respuesta de validación inválida"},
    },
)
def finish_timed_match(
    match_id: str,
    solution: SolutionDTO,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return finish_timed_match_use_case.execute(
            match_id,
            user_id,
            solution.positions,
        )
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except IncorrectSolution as exc:
        raise HTTPException(status_code=400, detail="Solución incorrecta") from exc
    except OwnMatch as exc:
        raise HTTPException(status_code=403, detail="No perteneces a esta partida") from exc
    except MatchUnavailable as exc:
        raise HTTPException(status_code=400, detail="La partida ya terminó") from exc
    except CaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CasesServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/matches/versus",
    response_model=VersusMatchDTO,
    status_code=200,
    summary="Crear partida versus",
    responses={
        200: {"description": "Partida versus creada"},
        409: {"description": "El usuario no tiene casos disponibles para jugar"},
        502: {"description": "Progresión o historial de usuario no disponible"},
        503: {"description": "Catálogo de casos o cola de emparejamiento no disponible"},
        422: {"description": "Solicitud inválida"},
    },
)
def create_versus_match(
    request: VersusMatchRequest,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.create_versus_match(user_id)
    except NoEligibleCase as exc:
        raise HTTPException(
            status_code=409,
            detail="No hay casos disponibles que puedas jugar y no hayas resuelto",
        ) from exc
    except MatchmakingBusy as exc:
        raise HTTPException(
            status_code=503,
            detail="El emparejamiento está ocupado. Reintentando…",
        ) from exc
    except CasesServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete(
    "/matches/versus/{match_id}",
    status_code=200,
    summary="Cancelar sala versus",
    responses={
        200: {"description": "Sala cancelada"},
        403: {"description": "Solo el creador puede cancelar la sala"},
        404: {"description": "Partida no encontrada"},
        409: {"description": "La sala ya no está esperando"},
        503: {"description": "La cola de emparejamiento está ocupada"},
    },
)
def cancel_versus_match(
    match_id: str,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.cancel_versus_match(match_id, user_id)
    except MatchmakingBusy as exc:
        raise HTTPException(
            status_code=503,
            detail="El emparejamiento está ocupado. Reintenta en unos segundos",
        ) from exc
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except OwnMatch as exc:
        raise HTTPException(status_code=403, detail="No puedes cancelar esta sala") from exc
    except MatchUnavailable as exc:
        raise HTTPException(status_code=409, detail="La sala ya no está esperando") from exc


@router.get(
    "/matches/versus/open",
    response_model=List[VersusMatchDTO],
    status_code=200,
    summary="Listar partidas versus disponibles",
    responses={200: {"description": "Partidas disponibles"}},
)
def list_open_matches(_user_id: str = Depends(get_current_user_id)):
    return match_service.list_open_matches()


@router.post(
    "/matches/versus/{match_id}/join",
    response_model=VersusMatchDTO,
    status_code=200,
    summary="Unirse a una partida versus",
    responses={
        400: {"description": "La partida no está disponible"},
        404: {"description": "Partida no encontrada"},
        409: {"description": "Los jugadores no comparten un caso elegible"},
        502: {"description": "Progresión o historial de usuario no disponible"},
        503: {"description": "Catálogo de casos o cola de emparejamiento no disponible"},
        200: {"description": "Partida unida correctamente"},
        422: {"description": "Solicitud inválida"},
    },
)
def join_versus_match(
    match_id: str,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.join_versus_match(match_id, user_id)
    except MatchmakingBusy as exc:
        raise HTTPException(
            status_code=503,
            detail="El emparejamiento está ocupado. Reintenta en unos segundos",
        ) from exc
    except NoEligibleCase as exc:
        raise HTTPException(
            status_code=409,
            detail="No hay un caso que ambos jugadores puedan jugar y no hayan resuelto",
        ) from exc
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
    except CaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CasesServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.report_progress(match_id, user_id, request.progress)
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except OwnMatch as exc:
        raise HTTPException(status_code=403, detail="No perteneces a esta partida") from exc
    except MatchUnavailable as exc:
        raise HTTPException(status_code=400, detail="La partida no está en curso") from exc


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
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.get_versus_progress(match_id, user_id)
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except OwnMatch as exc:
        raise HTTPException(
            status_code=403,
            detail="No perteneces a esta partida",
        ) from exc
    except MatchUnavailable as exc:
        raise HTTPException(
            status_code=400,
            detail="La partida aún no tiene dos participantes",
        ) from exc


@router.websocket("/matches/versus/{match_id}/progress/ws")
async def versus_progress_stream(websocket: WebSocket, match_id: str):
    """Authenticated push channel for progress updates to the two match players."""
    user_id = websocket.headers.get("X-User-Id")
    expected_secret = os.getenv("GATEWAY_SECRET", "")
    received_secret = websocket.headers.get("X-Gateway-Secret")
    if (
        not expected_secret
        or not has_valid_gateway_secret(received_secret, expected_secret)
        or user_id is None
        or not EXTERNAL_ID_PATTERN.fullmatch(user_id)
    ):
        await websocket.close(code=1008, reason="No autorizado")
        return

    try:
        snapshot = match_service.get_versus_progress(match_id, user_id)
    except (MatchNotFound, OwnMatch, MatchUnavailable):
        await websocket.close(code=1008, reason="Partida no disponible")
        return

    pubsub = redis_client.pubsub()
    try:
        pubsub.subscribe(f"match:{match_id}:progress:events")
        await websocket.accept()
        is_finished = snapshot.get("status") == "FINISHED"
        await websocket.send_json({
            "event": "finished" if is_finished else "snapshot",
            "progress": snapshot,
        })
        if is_finished:
            await websocket.close(code=1000)
            return

        heartbeat_loops = 0
        while True:
            message = await asyncio.to_thread(
                pubsub.get_message,
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message and message.get("type") == "message":
                snapshot = match_service.get_versus_progress(match_id, user_id)
                is_finished = snapshot.get("status") == "FINISHED"
                await websocket.send_json({
                    "event": "finished" if is_finished else "progress",
                    "progress": snapshot,
                })
                if is_finished:
                    await websocket.close(code=1000)
                    return
            heartbeat_loops += 1
            if heartbeat_loops >= 20:
                await websocket.send_json({"event": "heartbeat"})
                heartbeat_loops = 0
    except Exception:
        try:
            await websocket.close(code=1011, reason="Canal de progreso interrumpido")
        except Exception:
            pass
    finally:
        try:
            pubsub.unsubscribe(f"match:{match_id}:progress:events")
            pubsub.close()
        except Exception:
            pass
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
        502: {"description": "Error al registrar el resultado en usuarios"},
        503: {"description": "Casos no disponible o respuesta de validación inválida"},
    },
)
def submit_versus_solution(
    match_id: str,
    solution: SolutionDTO,
    user_id: str = Depends(get_current_user_id),
):
    try:
        return match_service.submit_versus_solution(
            match_id,
            user_id,
            solution.positions,
        )
    except MatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Partida no encontrada") from exc
    except IncorrectSolution as exc:
        raise HTTPException(status_code=400, detail="Solución incorrecta") from exc
    except OwnMatch as exc:
        raise HTTPException(status_code=403, detail="No perteneces a esta partida") from exc
    except MatchUnavailable as exc:
        raise HTTPException(status_code=400, detail="La partida no está en curso") from exc
    except CaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CasesServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
def get_ranking(
    limit: int = Query(10),
    _user_id: str = Depends(get_current_user_id),
):
    return match_service.get_ranking(limit)


@router.get(
    "/matches/history/me",
    response_model=List[MatchResultDTO],
    status_code=200,
    summary="Consultar historial del usuario actual",
    responses={
        200: {"description": "Historial de partidas del usuario"},
        403: {"description": "Encabezado X-User-Id inválido"},
    },
)
def get_my_history(user_id: str = Depends(get_current_user_id)):
    return match_service.get_user_history(user_id)


@router.get(
    "/matches/{match_id}/saga",
    response_model=SagaStatusDTO,
    summary="Consultar el estado de actualización del perfil por eventos",
)
def get_match_saga_status(
    match_id: str,
    user_id: str = Depends(get_current_user_id),
):
    match = match_repository.get_match(match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="Partida no encontrada")
    if user_id not in {match.get("player1"), match.get("player2")}:
        raise HTTPException(status_code=403, detail="No perteneces a esta partida")
    status = match_repository.get_saga_status(match_id)
    if status is None:
        raise HTTPException(status_code=409, detail="La partida aún no ha iniciado su saga")
    return status
