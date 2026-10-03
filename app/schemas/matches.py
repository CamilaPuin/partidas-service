from typing import Optional

from pydantic import BaseModel


class TimedMatchRequest(BaseModel):
    caseId: str
    timeLimitSeconds: int
    userId: str


class VersusMatchRequest(BaseModel):
    caseId: str
    userId: str


class JoinMatchRequest(BaseModel):
    userId: str


class MatchDTO(BaseModel):
    matchId: str
    caseId: Optional[str] = None
    status: Optional[str] = None
    startedAt: Optional[int] = None
    deadline: Optional[int] = None
    player1: Optional[str] = None
    player2: Optional[str] = None


class SolutionDTO(BaseModel):
    userId: str
    isCorrect: bool


class ResultDTO(BaseModel):
    won: bool
    timeSeconds: int
    bonus: int
    xpDelta: int


class VersusResultDTO(BaseModel):
    winnerId: str
    matchId: str
    xpDelta: int


class ProgressRequest(BaseModel):
    userId: str
    progress: float


class VersusProgressRequest(BaseModel):
    userId: str
    rivalId: str


class MatchHistoryRequest(BaseModel):
    userId: str


class RankingEntryDTO(BaseModel):
    userId: str
    score: int


class MatchResultDTO(BaseModel):
    matchId: str
    mode: str
    caseId: str
    result: str
    timestamp: int
