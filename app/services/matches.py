import time
import uuid
from typing import List

from app.core.service_clients import register_user_result, validate_case
from app.repositories.matches import MatchRepository
from app.schemas.matches import (
    MatchDTO,
    MatchResultDTO,
    ProgressRequest,
    RankingEntryDTO,
    ResultDTO,
    SolutionDTO,
    TimedMatchRequest,
    VersusMatchRequest,
    VersusResultDTO,
)


class MatchNotFound(Exception):
    pass


class MatchUnavailable(Exception):
    pass


class OwnMatch(Exception):
    pass


class IncorrectSolution(Exception):
    pass


class MatchService:
    def __init__(self, repository: MatchRepository):
        self.repository = repository

    def create_timed_match(
        self,
        request: TimedMatchRequest,
        user_id: int,
    ) -> MatchDTO:
        match_id = str(uuid.uuid4())
        now = int(time.time())
        deadline = now + request.timeLimitSeconds
        match_data = {
            "matchId": match_id,
            "caseId": request.caseId,
            "startedAt": now,
            "deadline": deadline,
            "mode": "TIMED",
            "status": "IN_PROGRESS",
            "player1": str(user_id),
        }
        self.repository.save_match(match_id, match_data)
        return MatchDTO(
            matchId=match_id,
            caseId=request.caseId,
            startedAt=now,
            deadline=deadline,
        )

    def finish_timed_match(
        self,
        match_id: str,
        user_id: int,
        solution: SolutionDTO,
    ) -> ResultDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data["player1"] != str(user_id):
            raise OwnMatch
        if match_data.get("status") == "FINISHED":
            return ResultDTO(**match_data["finalResult"])

        now = int(time.time())
        time_taken = now - match_data["startedAt"]
        is_correct = validate_case(match_data["caseId"], solution.positions)
        if now > match_data["deadline"] or not is_correct:
            result = ResultDTO(
                won=False,
                timeSeconds=time_taken,
                bonus=0,
                xpDelta=-10,
            )
        else:
            bonus = max(0, match_data["deadline"] - now) * 2
            result = ResultDTO(
                won=True,
                timeSeconds=time_taken,
                bonus=bonus,
                xpDelta=50 + bonus,
            )

        register_user_result(
            user_id,
            match_data["caseId"],
            "TIMED",
            result.won,
            time_taken,
            result.xpDelta,
        )
        self.repository.increment_score(str(user_id), result.xpDelta)
        self.repository.save_user_history(
            str(user_id),
            {
                "matchId": match_id,
                "mode": "TIMED",
                "caseId": match_data["caseId"],
                "result": "WON" if result.won else "LOST",
                "timestamp": now,
            },
        )
        match_data["status"] = "FINISHED"
        match_data["finalResult"] = result.dict()
        self.repository.save_match(match_id, match_data)
        return result

    def create_versus_match(
        self,
        request: VersusMatchRequest,
        user_id: int,
    ) -> MatchDTO:
        match_id = str(uuid.uuid4())
        match_data = {
            "matchId": match_id,
            "caseId": request.caseId,
            "status": "WAITING",
            "player1": str(user_id),
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
                        player1=int(data["player1"]) if data.get("player1") else None,
                    )
                )
        return matches

    def join_versus_match(self, match_id: str, user_id: int) -> MatchDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound
        if match_data["status"] != "WAITING":
            raise MatchUnavailable
        if match_data["player1"] == str(user_id):
            raise OwnMatch

        match_data["player2"] = str(user_id)
        match_data["status"] = "IN_PROGRESS"
        match_data["startedAt"] = int(time.time())
        self.repository.save_match(match_id, match_data)
        self.repository.remove_open_match(match_id)
        return MatchDTO(
            matchId=match_id,
            caseId=match_data["caseId"],
            status="IN_PROGRESS",
            player1=int(match_data["player1"]),
            player2=user_id,
        )

    def report_progress(
        self,
        match_id: str,
        user_id: int,
        request: ProgressRequest,
    ) -> dict:
        self.repository.save_progress(match_id, str(user_id), request.progress)
        return {"ok": True}

    def get_versus_progress(
        self,
        match_id: str,
        me_id: int,
        rival_id: int,
    ) -> dict:
        return {
            "me": self.repository.get_progress(match_id, str(me_id)),
            "rival": self.repository.get_progress(match_id, str(rival_id)),
        }

    def submit_versus_solution(
        self,
        match_id: str,
        user_id: int,
        solution: SolutionDTO,
    ) -> VersusResultDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound

        player_id = str(user_id)
        if player_id not in (match_data["player1"], match_data["player2"]):
            raise OwnMatch
        if match_data["status"] == "FINISHED":
            return VersusResultDTO(
                winnerId=int(match_data["winnerId"]),
                matchId=match_id,
                xpDelta=100 if player_id == match_data["winnerId"] else 0,
            )
        if match_data["status"] != "IN_PROGRESS":
            raise MatchUnavailable
        if not validate_case(match_data["caseId"], solution.positions):
            raise IncorrectSolution

        winner_id = player_id
        match_data["status"] = "FINISHED"
        match_data["winnerId"] = winner_id
        now = int(time.time())
        time_taken = now - match_data["startedAt"]
        players = [match_data["player1"], match_data["player2"]]
        for player in players:
            if player:
                won = player == winner_id
                xp_delta = 100 if won else 0
                register_user_result(
                    int(player),
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
        self.repository.save_match(match_id, match_data)
        return VersusResultDTO(
            winnerId=int(winner_id),
            matchId=match_id,
            xpDelta=100,
        )

    def get_ranking(self, limit: int) -> List[RankingEntryDTO]:
        entries = self.repository.list_ranking(limit)
        return [
            RankingEntryDTO(userId=int(user_id), score=int(score))
            for user_id, score in entries
        ]

    def get_user_history(self, user_id: int) -> List[MatchResultDTO]:
        return [
            MatchResultDTO(**entry)
            for entry in self.repository.list_user_history(str(user_id))
        ]
