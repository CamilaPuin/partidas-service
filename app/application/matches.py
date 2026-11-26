import json
import logging
import random
from typing import Any, Dict, List

from app.application.errors import (
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
from app.messaging.match_events import match_finished_event
from app.application.ports import (
    CasesGateway,
    Clock,
    MatchIdGenerator,
    MatchStore,
    UsersGateway,
)

logger = logging.getLogger(__name__)

class MatchService:
    """Application use cases for CLASSIC and VERSUS match flows."""

    def __init__(
        self,
        store: MatchStore,
        cases: CasesGateway,
        users: UsersGateway,
        clock: Clock,
        ids: MatchIdGenerator,
    ):
        self.repository = store
        self.cases = cases
        self.users = users
        self.clock = clock
        self.ids = ids

    def create_classic_match(
        self,
        case_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        case = self.cases.get_case_summary(case_id)
        difficulty = case["difficulty"]
        progression = self.users.get_progression(user_id)
        if progression["level"] < case["requiredLevel"]:
            raise MatchLocked
        match_id = self.ids.new_id()
        now = self.clock.now_seconds()
        match_data = {
            "matchId": match_id,
            "caseId": case_id,
            "difficulty": difficulty,
            "startedAt": now,
            "mode": "CLASSIC",
            "status": "IN_PROGRESS",
            "player1": user_id,
        }
        self.repository.save_match(match_id, match_data)
        return {"matchId": match_id, "caseId": case_id, "status": "IN_PROGRESS", "startedAt": now}

    def attempt_classic_match(
        self,
        match_id: str,
        user_id: str,
        attempt_id: str,
        positions: List[Any],
    ) -> Dict[str, Any]:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data.get("mode") != "CLASSIC" or match_data.get("player1") != user_id:
            raise OwnMatch

        positions = sorted(
            (position.model_dump() if hasattr(position, "model_dump") else dict(position) for position in positions),
            key=lambda position: (
                position["characterId"],
                position["x"],
                position["y"],
            ),
        )
        fingerprint = json.dumps(positions, sort_keys=True, separators=(",", ":"))
        reservation = self.repository.reserve_classic_attempt(
            match_id,
            attempt_id,
            fingerprint,
        )

        action = reservation["action"]
        if action == "not_found":
            raise MatchNotFound
        if action == "conflict":
            raise MatchConflict
        if action == "unavailable":
            raise MatchUnavailable
        if action == "replay":
            return reservation["result"]

        match_data = reservation["match"]
        if action == "validate":
            try:
                is_valid = self.cases.validate(match_data["caseId"], positions)
            except ServiceIntegrationError:
                self.repository.release_classic_validation(
                    match_id,
                    attempt_id,
                    fingerprint,
                )
                raise

            if not is_valid:
                result = {
                    "attemptId": attempt_id,
                    "valid": False,
                    "status": "IN_PROGRESS",
                }
                completion = self.repository.finish_classic_incorrect_attempt(
                    match_id,
                    attempt_id,
                    fingerprint,
                    result,
                )
                if completion["action"] == "replay":
                    return completion["result"]
                if completion["action"] == "conflict":
                    raise MatchConflict
                if completion["action"] != "stored":
                    raise MatchUnavailable
                return result

            finalized_at = self.clock.now_seconds()
            time_taken = max(0, finalized_at - match_data["startedAt"])
            finalizing = self.repository.mark_classic_finalizing(
                match_id,
                attempt_id,
                fingerprint,
                time_taken,
                finalized_at,
            )
            if finalizing["action"] == "replay":
                return finalizing["result"]
            if finalizing["action"] == "not_found":
                raise MatchNotFound
            if finalizing["action"] == "conflict":
                raise MatchConflict
            if finalizing["action"] != "finalize":
                raise MatchUnavailable
            match_data = finalizing["match"]
        elif action != "finalize":
            raise MatchUnavailable

        time_taken = match_data["finalizingTimeSeconds"]
        finalized_at = match_data["finalizingAt"]
        result = {
            "attemptId": attempt_id,
            "valid": True,
            "status": "FINISHED",
            "sagaStatus": "PENDING",
        }
        event = match_finished_event(
            match_id,
            match_data["caseId"],
            "CLASSIC",
            time_taken,
            [{
                "userId": user_id,
                "won": True,
                "xpDelta": None,
                "difficulty": match_data["difficulty"],
            }],
        )
        match_data.update({
            "sagaStatus": "PENDING",
            "sagaEventId": event["eventId"],
            "correlationId": event["correlationId"],
            "finishedAt": finalized_at,
            "matchFinishedEvent": event,
        })
        completion = self.repository.complete_classic_attempt(
            match_id,
            attempt_id,
            fingerprint,
            match_data,
            result,
            user_id,
            0,
            {
                "matchId": match_id,
                "mode": "CLASSIC",
                "caseId": match_data["caseId"],
                "result": "WON",
                "timestamp": finalized_at,
            },
        )
        if completion["action"] == "replay":
            return completion["result"]
        if completion["action"] == "not_found":
            raise MatchNotFound
        if completion["action"] == "conflict":
            raise MatchConflict
        if completion["action"] != "stored":
            raise MatchUnavailable
        return result

    def create_versus_match(
        self,
        user_id: str,
    ) -> Dict[str, Any]:
        try:
            with self.repository.versus_matchmaking_lock():
                if not self._eligible_versus_cases([user_id]):
                    raise NoEligibleCase

                # A prior request may have created the room even if its response
                # was lost. Reuse it instead of leaving the same player in two rooms.
                open_ids = sorted(self.repository.list_open_match_ids())
                for open_id in open_ids:
                    waiting = self.repository.get_match(open_id)
                    if not waiting or waiting.get("status") != "WAITING":
                        self.repository.remove_open_match(open_id)
                        continue
                    if waiting.get("player1") == user_id:
                        return self._waiting_versus_dto(waiting)

                # Joining under the same lock prevents two players who clicked
                # simultaneously from creating separate rooms for each other.
                for open_id in open_ids:
                    waiting = self.repository.get_match(open_id)
                    if (
                        not waiting
                        or waiting.get("status") != "WAITING"
                        or waiting.get("player1") == user_id
                    ):
                        continue
                    try:
                        return self._join_versus_match_locked(open_id, user_id)
                    except NoEligibleCase:
                        continue

                match_id = self.ids.new_id()
                match_data = {
                    "matchId": match_id,
                    "caseId": None,
                    "status": "WAITING",
                    "player1": user_id,
                    "player1Name": self.users.get_display_name(user_id),
                    "player2": None,
                    "player2Name": None,
                    "winnerId": None,
                    "mode": "VERSUS",
                }
                self.repository.save_match(match_id, match_data)
                self.repository.add_open_match(match_id)
                return self._waiting_versus_dto(match_data)
        except MatchmakingBusy:
            raise

    def list_open_matches(self) -> List[Dict[str, Any]]:
        matches = []
        for match_id in self.repository.list_open_match_ids():
            data = self.repository.get_match(match_id)
            if data:
                matches.append(
                    {
                        "matchId": data["matchId"],
                        "caseId": data["caseId"],
                        "status": data["status"],
                        "player1": data.get("player1"),
                        "player1Name": data.get("player1Name"),
                    }
                )
        return matches

    def join_versus_match(self, match_id: str, user_id: str) -> Dict[str, Any]:
        with self.repository.versus_matchmaking_lock():
            return self._join_versus_match_locked(match_id, user_id)

    def _join_versus_match_locked(self, match_id: str, user_id: str) -> Dict[str, Any]:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data["status"] != "WAITING":
            raise MatchUnavailable
        if match_data["player1"] == user_id:
            raise OwnMatch

        eligible_cases = self._eligible_versus_cases(
            [match_data["player1"], user_id]
        )
        if not eligible_cases:
            raise NoEligibleCase
        case = random.choice(eligible_cases)
        player2_name = self.users.get_display_name(user_id)
        match_data["player2"] = user_id
        match_data["player2Name"] = player2_name
        match_data["caseId"] = case["id"]
        match_data["status"] = "IN_PROGRESS"
        match_data["startedAt"] = self.clock.now_seconds()
        if not self.repository.complete_match(match_id, "WAITING", match_data):
            raise MatchUnavailable
        self.repository.remove_open_match(match_id)
        return {
            "matchId": match_id,
            "caseId": case["id"],
            "status": "IN_PROGRESS",
            "player1": match_data["player1"],
            "player2": user_id,
            "player1Name": match_data.get("player1Name"),
            "player2Name": player2_name,
            "startedAt": match_data["startedAt"],
        }

    @staticmethod
    def _waiting_versus_dto(match_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "matchId": match_data["matchId"],
            "caseId": None,
            "status": "WAITING",
            "player1": match_data["player1"],
            "player1Name": match_data.get("player1Name"),
        }

    def cancel_versus_match(self, match_id: str, user_id: str) -> Dict[str, Any]:
        with self.repository.versus_matchmaking_lock():
            return self._cancel_versus_match_locked(match_id, user_id)

    def _cancel_versus_match_locked(self, match_id: str, user_id: str) -> Dict[str, Any]:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data.get("mode") != "VERSUS" or match_data.get("player1") != user_id:
            raise OwnMatch
        if match_data.get("status") != "WAITING":
            raise MatchUnavailable
        match_data["status"] = "CANCELLED"
        match_data["cancelledAt"] = self.clock.now_seconds()
        if not self.repository.complete_match(match_id, "WAITING", match_data):
            raise MatchUnavailable
        self.repository.remove_open_match(match_id)
        return {"matchId": match_id, "status": "CANCELLED"}

    def report_progress(
        self,
        match_id: str,
        user_id: str,
        progress: float,
    ) -> dict:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if user_id not in (match_data.get("player1"), match_data.get("player2")):
            raise OwnMatch
        if match_data.get("mode") != "VERSUS" or match_data.get("status") != "IN_PROGRESS":
            raise MatchUnavailable
        self.repository.save_progress(match_id, user_id, progress)
        self._publish_progress(match_id, user_id, progress)
        return {"ok": True, "progress": progress}

    def _publish_progress(self, match_id: str, user_id: str, progress: float) -> None:
        try:
            self.repository.publish_progress(match_id, user_id, progress)
        except Exception:
            logger.warning("Could not publish versus progress matchId=%s", match_id)

    def get_versus_progress(
        self,
        match_id: str,
        me_id: str,
    ) -> dict:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound

        me_id_string = me_id
        if match_data.get("player1") == me_id_string:
            rival_id = match_data.get("player2")
        elif match_data.get("player2") == me_id_string:
            rival_id = match_data.get("player1")
        else:
            raise OwnMatch

        if not rival_id:
            raise MatchUnavailable

        return {
            "caseId": match_data["caseId"],
            "status": match_data.get("status"),
            "userId": me_id_string,
            "me": self.repository.get_progress(match_id, me_id_string),
            "rival": self.repository.get_progress(match_id, rival_id),
            "opponentName": (
                match_data.get("player2Name")
                if match_data.get("player1") == me_id_string
                else match_data.get("player1Name")
            ),
            "player1": match_data.get("player1"),
            "player2": match_data.get("player2"),
            "player1Name": match_data.get("player1Name"),
            "player2Name": match_data.get("player2Name"),
            "startedAt": match_data.get("startedAt"),
            "winnerId": match_data.get("winnerId"),
            "winnerName": (
                match_data.get("player1Name")
                if match_data.get("winnerId") == match_data.get("player1")
                else match_data.get("player2Name")
            ),
            "xpDelta": 100 if match_data.get("winnerId") == me_id_string else 0,
            "sagaStatus": match_data.get("sagaStatus"),
            "timeSeconds": match_data.get("timeSeconds"),
            "playerTimes": match_data.get("playerTimes", []),
            "solution": match_data.get("solution", []),
        }

    def _eligible_versus_cases(self, user_ids: List[str]) -> List[Dict[str, Any]]:
        progressions = [self.users.get_progression(user_id) for user_id in user_ids]
        max_required_level = min(progression["level"] for progression in progressions)
        solved_case_ids = {
            case_id
            for user_id in user_ids
            for case_id in self.users.get_solved_case_ids(user_id)
        }
        return [
            case
            for case in self.cases.list_case_summaries()
            if case["requiredLevel"] <= max_required_level
            and case["id"] not in solved_case_ids
        ]

    def submit_versus_solution(
        self,
        match_id: str,
        user_id: str,
        positions: List[Any],
    ) -> Dict[str, Any]:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound

        player_id = user_id
        if player_id not in (match_data["player1"], match_data["player2"]):
            raise OwnMatch
        if match_data["status"] != "IN_PROGRESS":
            raise MatchUnavailable
        if not self.cases.validate(match_data["caseId"], positions):
            raise IncorrectSolution

        winner_id = player_id
        match_data["status"] = "FINISHED"
        match_data["winnerId"] = winner_id
        now = self.clock.now_seconds()
        time_taken = max(0, now - int(match_data.get("startedAt", now)))
        players = [match_data["player1"], match_data["player2"]]
        results = [
            {
                "userId": player,
                "won": player == winner_id,
                "xpDelta": 100 if player == winner_id else 0,
                "difficulty": None,
            }
            for player in players
            if player
        ]
        event = match_finished_event(
            match_id, match_data["caseId"], "VERSUS", time_taken, results
        )
        solution = [
            position.model_dump() if hasattr(position, "model_dump") else dict(position)
            for position in positions
        ]
        player_times = [
            {
                "userId": player,
                "displayName": (
                    match_data.get("player1Name")
                    if player == match_data.get("player1")
                    else match_data.get("player2Name")
                ),
                "timeSeconds": time_taken if player == winner_id else None,
            }
            for player in players
            if player
        ]
        match_data.update({
            "sagaStatus": "PENDING",
            "sagaEventId": event["eventId"],
            "correlationId": event["correlationId"],
            "finishedAt": now,
            "timeSeconds": time_taken,
            "solution": solution,
            "playerTimes": player_times,
            "matchFinishedEvent": event,
        })
        if not self.repository.complete_match(
            match_id,
            "IN_PROGRESS",
            match_data,
        ):
            raise MatchUnavailable
        self._publish_progress(match_id, player_id, 1.0)

        return {
            "winnerId": winner_id,
            "matchId": match_id,
            "xpDelta": 100,
            "sagaStatus": "PENDING",
            "caseId": match_data["caseId"],
            "player1": match_data.get("player1"),
            "player2": match_data.get("player2"),
            "player1Name": match_data.get("player1Name"),
            "player2Name": match_data.get("player2Name"),
            "winnerName": (
                match_data.get("player1Name")
                if winner_id == match_data.get("player1")
                else match_data.get("player2Name")
            ),
            "timeSeconds": time_taken,
            "playerTimes": player_times,
            "solution": solution,
        }

    def get_ranking(self, limit: int) -> List[Dict[str, Any]]:
        entries = self.repository.list_ranking(limit)
        return [
            {"userId": user_id, "score": int(score), "rank": index}
            for index, (user_id, score) in enumerate(entries, start=1)
        ]

    def get_user_history(self, user_id: str) -> List[Dict[str, Any]]:
        return self.repository.list_user_history(user_id)
