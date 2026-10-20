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


@router.post("/matches/timed", response_model=MatchDTO)
def create_timed_match(
    request: TimedMatchRequest,
    user_id: int = Header(..., alias="X-User-Id"),
):
    return match_service.create_timed_match(request, user_id)


@router.post("/matches/timed/{match_id}/finish", response_model=ResultDTO)
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
    except ServiceIntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/matches/versus", response_model=MatchDTO)
def create_versus_match(
    request: VersusMatchRequest,
    user_id: int = Header(..., alias="X-User-Id"),
):
    return match_service.create_versus_match(request, user_id)


@router.get("/matches/versus/open", response_model=List[MatchDTO])
def list_open_matches():
    return match_service.list_open_matches()


@router.post("/matches/versus/{match_id}/join", response_model=MatchDTO)
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


@router.put("/matches/versus/{match_id}/progress")
def report_progress(
    match_id: str,
    request: ProgressRequest,
    user_id: int = Header(..., alias="X-User-Id"),
):
    return match_service.report_progress(match_id, user_id, request)


@router.get("/matches/versus/{match_id}/progress")
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


@router.post("/matches/versus/{match_id}/submit", response_model=VersusResultDTO)
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


@router.get("/matches/ranking", response_model=List[RankingEntryDTO])
def get_ranking(limit: int = Query(10)):
    return match_service.get_ranking(limit)


@router.get("/matches/history/me", response_model=List[MatchResultDTO])
def get_my_history(user_id: int = Header(..., alias="X-User-Id")):
    return match_service.get_user_history(user_id)
