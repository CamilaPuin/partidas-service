import json
import time
import uuid
from typing import List

from app.application.errors import (
    CaseNotFound,
    CasesServiceError,
    IncorrectSolution,
    MatchConflict,
    MatchNotFound,
    MatchUnavailable,
    OwnMatch,
    ServiceIntegrationError,
    UserResultConflict,
)
from app.core.service_clients import (
    get_case_difficulty,
    register_user_result,
    validate_case,
)
from app.repositories.matches import MatchRepository
from app.schemas.matches import (
    ClassicAttemptRequest,
    ClassicAttemptResultDTO,
    ClassicMatchRequest,
    MatchDTO,
    MatchResultDTO,
    ProgressRequest,
    RankingEntryDTO,
    SolutionDTO,
    VersusMatchRequest,
    VersusResultDTO,
)


class MatchService:
    def __init__(self, repository: MatchRepository):
        self.repository = repository

    def create_classic_match(
        self,
        request: ClassicMatchRequest,
        user_id: str,
    ) -> MatchDTO:
        difficulty = get_case_difficulty(request.caseId)
        match_id = str(uuid.uuid4())
        now = int(time.time())
        match_data = {
            "matchId": match_id,
            "caseId": request.caseId,
            "difficulty": difficulty,
            "startedAt": now,
            "mode": "CLASSIC",
            "status": "IN_PROGRESS",
            "player1": user_id,
        }
        self.repository.save_match(match_id, match_data)
        return MatchDTO(
            matchId=match_id,
            caseId=request.caseId,
            status="IN_PROGRESS",
            startedAt=now,
        )

    def attempt_classic_match(
        self,
        match_id: str,
        user_id: str,
        request: ClassicAttemptRequest,
    ) -> ClassicAttemptResultDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data.get("mode") != "CLASSIC" or match_data.get("player1") != user_id:
            raise OwnMatch

        attempt_id = str(request.attemptId)
        positions = sorted(
            (position.model_dump() for position in request.positions),
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
            return ClassicAttemptResultDTO(**reservation["result"])

        match_data = reservation["match"]
        if action == "validate":
            try:
                is_valid = validate_case(match_data["caseId"], request.positions)
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
                    return ClassicAttemptResultDTO(**completion["result"])
                if completion["action"] == "conflict":
                    raise MatchConflict
                if completion["action"] != "stored":
                    raise MatchUnavailable
                return ClassicAttemptResultDTO(**result)

            finalized_at = int(time.time())
            time_taken = max(0, finalized_at - match_data["startedAt"])
            finalizing = self.repository.mark_classic_finalizing(
                match_id,
                attempt_id,
                fingerprint,
                time_taken,
                finalized_at,
            )
            if finalizing["action"] == "replay":
                return ClassicAttemptResultDTO(**finalizing["result"])
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
        try:
            user_result = register_user_result(
                user_id,
                match_id,
                match_data["caseId"],
                "CLASSIC",
                True,
                time_taken,
                None,
                match_data["difficulty"],
            )
        except UserResultConflict as exc:
            raise MatchConflict from exc

        result = {
            "attemptId": attempt_id,
            "valid": True,
            "status": "FINISHED",
            "xpDelta": user_result["xpDelta"],
            "firstWinRewarded": user_result["firstWinRewarded"],
        }
        completion = self.repository.complete_classic_attempt(
            match_id,
            attempt_id,
            fingerprint,
            match_data,
            result,
            user_id,
            user_result["xpDelta"],
            {
                "matchId": match_id,
                "mode": "CLASSIC",
                "caseId": match_data["caseId"],
                "result": "WON",
                "timestamp": finalized_at,
            },
        )
        if completion["action"] == "replay":
            return ClassicAttemptResultDTO(**completion["result"])
        if completion["action"] == "not_found":
            raise MatchNotFound
        if completion["action"] == "conflict":
            raise MatchConflict
        if completion["action"] != "stored":
            raise MatchUnavailable
        return ClassicAttemptResultDTO(**result)

    def create_versus_match(
        self,
        request: VersusMatchRequest,
        user_id: str,
    ) -> MatchDTO:
        match_id = str(uuid.uuid4())
        match_data = {
            "matchId": match_id,
            "caseId": request.caseId,
            "status": "WAITING",
            "player1": user_id,
            "player2": None,
            "winnerId": None,
            "mode": "VERSUS",
        }
        self.repository.save_match(match_id, match_data)
        self.repository.add_open_match(match_id)
        return MatchDTO(
            matchId=match_id,
            caseId=request.caseId,
            status="WAITING",
            player1=user_id,
        )

    def list_open_matches(self) -> List[MatchDTO]:
        matches = []
        for match_id in self.repository.list_open_match_ids():
            data = self.repository.get_match(match_id)
            if data:
                matches.append(
                    MatchDTO(
                        matchId=data["matchId"],
                        caseId=data["caseId"],
                        status=data["status"],
                        player1=data.get("player1"),
                    )
                )
        return matches

    def join_versus_match(self, match_id: str, user_id: str) -> MatchDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data["status"] != "WAITING":
            raise MatchUnavailable
        if match_data["player1"] == user_id:
            raise OwnMatch

        match_data["player2"] = user_id
        match_data["status"] = "IN_PROGRESS"
        match_data["startedAt"] = int(time.time())
        self.repository.save_match(match_id, match_data)
        self.repository.remove_open_match(match_id)
        return MatchDTO(
            matchId=match_id,
            caseId=match_data["caseId"],
            status="IN_PROGRESS",
            player1=match_data["player1"],
            player2=user_id,
        )

    def report_progress(
        self,
        match_id: str,
        user_id: str,
        request: ProgressRequest,
    ) -> dict:
        self.repository.save_progress(match_id, user_id, request.progress)
        return {"ok": True}

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
            "me": self.repository.get_progress(match_id, me_id_string),
            "rival": self.repository.get_progress(match_id, rival_id),
        }

    def submit_versus_solution(
        self,
        match_id: str,
        user_id: str,
        solution: SolutionDTO,
    ) -> VersusResultDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound

        player_id = user_id
        if player_id not in (match_data["player1"], match_data["player2"]):
            raise OwnMatch
        if match_data["status"] != "IN_PROGRESS":
            raise MatchUnavailable
        if not validate_case(match_data["caseId"], solution.positions):
            raise IncorrectSolution

        winner_id = player_id
        match_data["status"] = "FINISHED"
        match_data["winnerId"] = winner_id
        if not self.repository.complete_match(
            match_id,
            "IN_PROGRESS",
            match_data,
        ):
            raise MatchUnavailable

        now = int(time.time())
        time_taken = now - match_data["startedAt"]
        players = [match_data["player1"], match_data["player2"]]
        for player in players:
            if player:
                won = player == winner_id
                xp_delta = 100 if won else 0
                register_user_result(
                    player,
                    match_id,
                    match_data["caseId"],
                    "VERSUS",
                    won,
                    time_taken,
                    xp_delta,
                )

        for player in players:
            if player:
                won = player == winner_id
                xp_delta = 100 if won else 0
                self.repository.save_user_history(
                    player,
                    {
                        "matchId": match_id,
                        "mode": "VERSUS",
                        "caseId": match_data["caseId"],
                        "result": "WON" if won else "LOST",
                        "timestamp": now,
                    },
                )
                if won:
                    self.repository.increment_score(player, xp_delta)
        return VersusResultDTO(
            winnerId=winner_id,
            matchId=match_id,
            xpDelta=100,
        )

    def get_ranking(self, limit: int) -> List[RankingEntryDTO]:
        entries = self.repository.list_ranking(limit)
        return [
            RankingEntryDTO(userId=user_id, score=int(score), rank=index)
            for index, (user_id, score) in enumerate(entries, start=1)
        ]

    def get_user_history(self, user_id: str) -> List[MatchResultDTO]:
        return [
            MatchResultDTO(**entry)
            for entry in self.repository.list_user_history(user_id)
        ]
