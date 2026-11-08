from typing import Annotated, Optional

from pydantic import BaseModel, Field, StrictInt, StrictStr, StringConstraints


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


class PositionDTO(BaseModel):
    characterId: Annotated[StrictStr, StringConstraints(strip_whitespace=True, min_length=1)]
    x: Annotated[StrictInt, Field(ge=0)]
    y: Annotated[StrictInt, Field(ge=0)]


class SolutionDTO(BaseModel):
    positions: Annotated[list[PositionDTO], Field(min_length=1)]


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
    rank: int


class MatchResultDTO(BaseModel):
    matchId: str
    mode: str
    caseId: str
    result: str
    timestamp: int
