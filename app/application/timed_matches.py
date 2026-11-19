from typing import Any, Dict, List

from app.application.errors import MatchLocked, MatchNotFound, MatchUnavailable, OwnMatch
from app.application.ports import (
    CaseSolutionValidator,
    CasesGateway,
    Clock,
    MatchIdGenerator,
    TimedMatchStore,
    UserProgressionProvider,
)
from app.domain.timed_match import IN_PROGRESS, calculate_outcome, finish_match_data
from app.messaging.match_events import match_finished_event


class CreateTimedMatch:
    def __init__(
        self,
        store: TimedMatchStore,
        cases: CasesGateway,
        users: UserProgressionProvider,
        clock: Clock,
        ids: MatchIdGenerator,
    ) -> None:
        self._store = store
        self._cases = cases
        self._users = users
        self._clock = clock
        self._ids = ids

    def execute(self, case_id: str, time_limit_seconds: int, user_id: str) -> Dict[str, Any]:
        case = self._cases.get_case_summary(case_id)
        progression = self._users.get_progression(user_id)
        if progression["level"] < case["requiredLevel"]:
            raise MatchLocked
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
        users: Any,
        clock: Clock,
    ) -> None:
        self._store = store
        self._cases = cases
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
        event = match_finished_event(
            match_id,
            match_data["caseId"],
            "TIMED",
            outcome.time_seconds,
            [{
                "userId": user_id,
                "won": outcome.won,
                "xpDelta": outcome.xp_delta,
                "difficulty": None,
            }],
        )
        final_match = finish_match_data(match_data, outcome)
        final_match.update({
            "sagaStatus": "PENDING",
            "sagaEventId": event["eventId"],
            "correlationId": event["correlationId"],
            "finishedAt": now,
            "matchFinishedEvent": event,
        })
        if not self._store.complete_match(
            match_id,
            IN_PROGRESS,
            final_match,
        ):
            raise MatchUnavailable
        response = outcome.to_dict()
        response["sagaStatus"] = "PENDING"
        return response
