from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class AppliedUserResult:
    xp_delta: int
    first_win_rewarded: bool


class MatchStore(Protocol):
    def get_match(self, match_id: str) -> Optional[Dict[str, Any]]: ...

    def save_match(self, match_id: str, match_data: Dict[str, Any]) -> None: ...

    def complete_match(
        self,
        match_id: str,
        expected_status: str,
        match_data: Dict[str, Any],
    ) -> bool: ...

    def get_saga_status(self, match_id: str) -> Optional[Dict[str, Any]]: ...

    def claim_match_event(self) -> Optional[str]: ...

    def acknowledge_match_event(self, raw_event: str) -> None: ...

    def requeue_match_event(self, raw_event: str) -> None: ...

    def apply_saga_outcome(
        self, match_id: str, correlation_id: str, status: str,
        results: List[Dict[str, Any]],
    ) -> str: ...

    def increment_score(self, user_id: str, delta: int) -> None: ...

    def save_user_history(self, user_id: str, history_entry: Dict[str, Any]) -> None: ...

    def list_user_history(self, user_id: str) -> List[Dict[str, Any]]: ...

    def list_ranking(self, limit: int) -> List[Any]: ...

    def add_open_match(self, match_id: str) -> None: ...

    def remove_open_match(self, match_id: str) -> None: ...

    def list_open_match_ids(self) -> Any: ...

    def save_progress(self, match_id: str, user_id: str, progress: float) -> None: ...

    def get_progress(self, match_id: str, user_id: str) -> float: ...

    def reserve_classic_attempt(
        self, match_id: str, attempt_id: str, fingerprint: str
    ) -> Dict[str, Any]: ...

    def release_classic_validation(
        self, match_id: str, attempt_id: str, fingerprint: str
    ) -> bool: ...

    def mark_classic_finalizing(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
        time_seconds: int,
        finalized_at: int,
    ) -> Dict[str, Any]: ...

    def finish_classic_incorrect_attempt(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]: ...

    def complete_classic_attempt(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
        match_data: Dict[str, Any],
        result: Dict[str, Any],
        user_id: str,
        score_delta: int,
        history_entry: Dict[str, Any],
    ) -> Dict[str, Any]: ...


class CaseSolutionValidator(Protocol):
    def validate(self, case_id: str, positions: List[Any]) -> bool: ...


class CaseDifficultyProvider(Protocol):
    def get_difficulty(self, case_id: str) -> str: ...


class UserProgressionProvider(Protocol):
    def get_progression(self, user_id: str) -> Dict[str, Any]: ...


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


class CasesGateway(CaseSolutionValidator, CaseDifficultyProvider, Protocol):
    pass


class UsersGateway(UserResultRecorder, UserProgressionProvider, Protocol):
    pass


# Kept as an import alias for existing timed-match integrations.
TimedMatchStore = MatchStore


class Clock(Protocol):
    def now_seconds(self) -> int: ...


class MatchIdGenerator(Protocol):
    def new_id(self) -> str: ...
