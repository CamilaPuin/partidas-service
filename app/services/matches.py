import time
import uuid
from typing import List

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
        user_id: str,
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
            "player1": user_id,
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
        solution: SolutionDTO,
    ) -> ResultDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound

        now = int(time.time())
        time_taken = now - match_data["startedAt"]
        if now > match_data["deadline"] or not solution.isCorrect:
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

        self.repository.increment_score(solution.userId, result.xpDelta)
        self.repository.save_user_history(
            solution.userId,
            {
                "matchId": match_id,
                "mode": "TIMED",
                "caseId": match_data["caseId"],
                "result": "WON" if result.won else "LOST",
                "timestamp": now,
            },
        )
        return result

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
        self.repository.save_match(match_id, match_data)
        self.repository.remove_open_match(match_id)
        return MatchDTO(
            matchId=match_id,
            caseId=match_data["caseId"],
            status="IN_PROGRESS",
            player1=match_data["player1"],
            player2=user_id,
        )

    def report_progress(self, match_id: str, request: ProgressRequest) -> dict:
        self.repository.save_progress(match_id, request.userId, request.progress)
        return {"ok": True}

    def get_versus_progress(
        self,
        match_id: str,
        me_id: str,
        rival_id: str,
    ) -> dict:
        return {
            "me": self.repository.get_progress(match_id, me_id),
            "rival": self.repository.get_progress(match_id, rival_id),
        }

    def submit_versus_solution(
        self,
        match_id: str,
        solution: SolutionDTO,
    ) -> VersusResultDTO:
        match_data = self.repository.get_match(match_id)
        if not match_data:
            raise MatchNotFound

        if match_data["status"] == "FINISHED":
            return VersusResultDTO(
                winnerId=match_data["winnerId"],
                matchId=match_id,
                xpDelta=100 if solution.userId == match_data["winnerId"] else 0,
            )
        if not solution.isCorrect:
            raise IncorrectSolution

        winner_id = solution.userId
        match_data["status"] = "FINISHED"
        match_data["winnerId"] = winner_id
        self.repository.save_match(match_id, match_data)
        self.repository.increment_score(winner_id, 100)

        now = int(time.time())
        for player in [match_data["player1"], match_data["player2"]]:
            if player:
                self.repository.save_user_history(
                    player,
                    {
                        "matchId": match_id,
                        "mode": "VERSUS",
                        "caseId": match_data["caseId"],
                        "result": "WON" if player == winner_id else "LOST",
                        "timestamp": now,
                    },
                )
        return VersusResultDTO(
            winnerId=winner_id,
            matchId=match_id,
            xpDelta=100,
        )

    def get_ranking(self, limit: int) -> List[RankingEntryDTO]:
        entries = self.repository.list_ranking(limit)
        return [
            RankingEntryDTO(userId=user_id, score=int(score))
            for user_id, score in entries
        ]

    def get_user_history(self, user_id: str) -> List[MatchResultDTO]:
        return [
            MatchResultDTO(**entry)
            for entry in self.repository.list_user_history(user_id)
        ]
