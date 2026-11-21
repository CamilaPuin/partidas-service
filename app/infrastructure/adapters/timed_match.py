import time
import uuid
from typing import Any, List

from app.core.service_clients import (
    get_case_summary,
    get_case_difficulty,
    get_solved_case_ids,
    get_user_display_name,
    list_case_summaries,
    get_user_progression,
    register_user_result,
    validate_case,
)
from app.application.ports import AppliedUserResult


class SystemClock:
    def now_seconds(self) -> int:
        return int(time.time())


class UuidMatchIdGenerator:
    def new_id(self) -> str:
        return str(uuid.uuid4())


class CasesHttpAdapter:
    def get_difficulty(self, case_id: str) -> str:
        return get_case_difficulty(case_id)

    def get_case_summary(self, case_id: str) -> dict:
        return get_case_summary(case_id)

    def list_case_summaries(self) -> list[dict]:
        return list_case_summaries()

    def validate(self, case_id: str, positions: List[Any]) -> bool:
        return validate_case(case_id, positions)


class UsersHttpAdapter:
    def get_progression(self, user_id: str) -> dict:
        return get_user_progression(user_id)

    def get_solved_case_ids(self, user_id: str) -> list[str]:
        return get_solved_case_ids(user_id)

    def get_display_name(self, user_id: str) -> str:
        return get_user_display_name(user_id)

    def register(
        self,
        user_id: str,
        match_id: str,
        case_id: str,
        mode: str,
        won: bool,
        time_seconds: int,
        xp_delta: int,
    ) -> AppliedUserResult:
        applied = register_user_result(
            user_id,
            match_id,
            case_id,
            mode,
            won,
            time_seconds,
            xp_delta,
            None,
        )
        return AppliedUserResult(
            xp_delta=applied["xpDelta"],
            first_win_rewarded=applied["firstWinRewarded"],
        )
