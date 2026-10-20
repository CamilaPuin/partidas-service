from typing import Optional

from pydantic import BaseModel


class TimedMatchRequest(BaseModel):
    caseId: str
    timeLimitSeconds: int


class VersusMatchRequest(BaseModel):
    caseId: str


class MatchDTO(BaseModel):
    matchId: str
    caseId: Optional[str] = None
    status: Optional[str] = None
    startedAt: Optional[int] = None
    deadline: Optional[int] = None
    player1: Optional[int] = None
    player2: Optional[int] = None


class SolutionDTO(BaseModel):
    positions: list[int]


class ResultDTO(BaseModel):
    won: bool
    timeSeconds: int
    bonus: int
    xpDelta: int


class VersusResultDTO(BaseModel):
    winnerId: int
    matchId: str
    xpDelta: int


class ProgressRequest(BaseModel):
    progress: float


class RankingEntryDTO(BaseModel):
    userId: int
    score: int


class MatchResultDTO(BaseModel):
    matchId: str
    mode: str
    caseId: str
    result: str
    timestamp: int
