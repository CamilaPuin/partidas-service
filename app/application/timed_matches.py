from typing import Any, Dict, List

from app.application.errors import MatchNotFound, MatchUnavailable, OwnMatch
from app.application.ports import (
    CaseSolutionValidator,
    Clock,
    MatchIdGenerator,
    TimedMatchStore,
    UserResultRecorder,
)
from app.domain.timed_match import IN_PROGRESS, calculate_outcome, finish_match_data


class CreateTimedMatch:
    def __init__(
        self,
        store: TimedMatchStore,
        clock: Clock,
        ids: MatchIdGenerator,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    def execute(self, case_id: str, time_limit_seconds: int, user_id: str) -> Dict[str, Any]:
        match_id = self._ids.new_id()
        now = self._clock.now_seconds()
        deadline = now + time_limit_seconds
        match_data = {
            "matchId": match_id,
            "caseId": case_id,
            "startedAt": now,
            "deadline": deadline,
            "mode": "TIMED",
            "status": IN_PROGRESS,
            "player1": user_id,
        }
        self._store.save_match(match_id, match_data)
        return {
            "matchId": match_id,
            "caseId": case_id,
            "startedAt": now,
            "deadline": deadline,
        }


class FinishTimedMatch:
    def __init__(
        self,
        store: TimedMatchStore,
        cases: CaseSolutionValidator,
        users: UserResultRecorder,
        clock: Clock,
    ) -> None:
        self._store = store
        self._cases = cases
        self._users = users
        self._clock = clock

    def execute(self, match_id: str, user_id: str, positions: List[Any]) -> Dict[str, Any]:
        match_data = self._store.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data["player1"] != user_id:
            raise OwnMatch
        if match_data.get("status") != IN_PROGRESS:
            raise MatchUnavailable

        now = self._clock.now_seconds()
        # Preserve existing behavior: case validation happens even after the deadline.
        is_correct = self._cases.validate(match_data["caseId"], positions)
        outcome = calculate_outcome(
            match_data["startedAt"],
            match_data["deadline"],
            now,
            is_correct,
        )
        if not self._store.complete_match(
            match_id,
            IN_PROGRESS,
            finish_match_data(match_data, outcome),
        ):
            raise MatchUnavailable

        self._users.register(
            user_id,
            match_data["caseId"],
            "TIMED",
            outcome.won,
            outcome.time_seconds,
            outcome.xp_delta,
        )
        self._store.increment_score(user_id, outcome.xp_delta)
        self._store.save_user_history(
            user_id,
            {
                "matchId": match_id,
                "mode": "TIMED",
                "caseId": match_data["caseId"],
                "result": "WON" if outcome.won else "LOST",
                "timestamp": now,
            },
        )
        return outcome.to_dict()
