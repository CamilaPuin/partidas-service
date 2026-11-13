from dataclasses import dataclass
from typing import Any, Dict


IN_PROGRESS = "IN_PROGRESS"
FINISHED = "FINISHED"


@dataclass(frozen=True)
class TimedOutcome:
    won: bool
    time_seconds: int
    bonus: int
    xp_delta: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "won": self.won,
            "timeSeconds": self.time_seconds,
            "bonus": self.bonus,
            "xpDelta": self.xp_delta,
        }


def elapsed_seconds(started_at: int, now: int) -> int:
    return now - started_at


def deadline_has_expired(deadline: int, now: int) -> bool:
    return now > deadline


def calculate_outcome(
    started_at: int,
    deadline: int,
    now: int,
    solution_is_correct: bool,
) -> TimedOutcome:
    time_taken = elapsed_seconds(started_at, now)
    if deadline_has_expired(deadline, now) or not solution_is_correct:
        return TimedOutcome(
            won=False,
            time_seconds=time_taken,
            bonus=0,
            xp_delta=-10,
        )

    bonus = max(0, deadline - now) * 2
    return TimedOutcome(
        won=True,
        time_seconds=time_taken,
        bonus=bonus,
        xp_delta=50 + bonus,
    )


def finish_match_data(match_data: Dict[str, Any], outcome: TimedOutcome) -> Dict[str, Any]:
    finished = dict(match_data)
    finished["status"] = FINISHED
    finished["finalResult"] = outcome.to_dict()
    return finished
