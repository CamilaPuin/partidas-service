import time
import uuid
from typing import List

from app.application.errors import (
    IncorrectSolution,
    MatchNotFound,
    MatchUnavailable,
    OwnMatch,
)
from app.core.service_clients import register_user_result, validate_case
from app.repositories.matches import MatchRepository
from app.schemas.matches import (
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
