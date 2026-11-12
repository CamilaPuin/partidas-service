from typing import Annotated, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)


class ClassicMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caseId: Annotated[
        StrictStr,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]


class TimedMatchRequest(BaseModel):
    caseId: str
    timeLimitSeconds: Annotated[int, Field(ge=60, le=3600)]


class VersusMatchRequest(BaseModel):
    caseId: str


class MatchDTO(BaseModel):
    matchId: str
    caseId: Optional[str] = None
    status: Optional[str] = None
    startedAt: Optional[int] = None
    deadline: Optional[int] = None
    player1: Optional[str] = None
    player2: Optional[str] = None


class PositionDTO(BaseModel):
    characterId: Annotated[StrictStr, StringConstraints(strip_whitespace=True, min_length=1)]
    x: Annotated[StrictInt, Field(ge=0)]
    y: Annotated[StrictInt, Field(ge=0)]


class SolutionDTO(BaseModel):
    positions: Annotated[list[PositionDTO], Field(min_length=1)]


class ClassicAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attemptId: UUID
    positions: Annotated[list[PositionDTO], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_unique_characters(self):
        character_ids = [position.characterId for position in self.positions]
        if len(set(character_ids)) != len(character_ids):
            raise ValueError("La petición repite identificadores de personaje")
        return self


class ResultDTO(BaseModel):
    won: bool
    timeSeconds: int
    bonus: int
    xpDelta: int


class ClassicAttemptResultDTO(BaseModel):
    attemptId: str
    valid: bool
    status: str
    xpDelta: Optional[int] = None
    firstWinRewarded: Optional[bool] = None


class VersusResultDTO(BaseModel):
    winnerId: str
    matchId: str
    xpDelta: int


class ProgressRequest(BaseModel):
    progress: float


class RankingEntryDTO(BaseModel):
    userId: str
    score: int
    rank: int


class MatchResultDTO(BaseModel):
    matchId: str
    mode: str
    caseId: str
    result: str
    timestamp: int
