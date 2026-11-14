import time
import uuid
from typing import Any, List

from app.core.service_clients import register_user_result, validate_case
from app.application.ports import AppliedUserResult


class SystemClock:
    def now_seconds(self) -> int:
        return int(time.time())


class UuidMatchIdGenerator:
    def new_id(self) -> str:
        return str(uuid.uuid4())


class CasesHttpAdapter:
    def validate(self, case_id: str, positions: List[Any]) -> bool:
        return validate_case(case_id, positions)


class UsersHttpAdapter:
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
