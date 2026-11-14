from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class AppliedUserResult:
    xp_delta: int
    first_win_rewarded: bool


class TimedMatchStore(Protocol):
    def get_match(self, match_id: str) -> Optional[Dict[str, Any]]: ...

    def save_match(self, match_id: str, match_data: Dict[str, Any]) -> None: ...

    def complete_match(
        self,
        match_id: str,
        expected_status: str,
        match_data: Dict[str, Any],
    ) -> bool: ...

    def increment_score(self, user_id: str, delta: int) -> None: ...

    def save_user_history(self, user_id: str, history_entry: Dict[str, Any]) -> None: ...


class CaseSolutionValidator(Protocol):
    def validate(self, case_id: str, positions: List[Any]) -> bool: ...


class UserResultRecorder(Protocol):
    def register(
        self,
        user_id: str,
        match_id: str,
        case_id: str,
        mode: str,
        won: bool,
        time_seconds: int,
        xp_delta: int,
    ) -> AppliedUserResult: ...


class Clock(Protocol):
    def now_seconds(self) -> int: ...


class MatchIdGenerator(Protocol):
    def new_id(self) -> str: ...
