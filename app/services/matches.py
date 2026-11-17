"""Backward-compatible import path for the application match use cases."""

from app.application.errors import (
    IncorrectSolution,
    MatchConflict,
    MatchLocked,
    MatchNotFound,
    MatchUnavailable,
    OwnMatch,
)
from app.application.matches import MatchService

__all__ = [
    "IncorrectSolution",
    "MatchConflict",
    "MatchLocked",
    "MatchNotFound",
    "MatchService",
    "MatchUnavailable",
    "OwnMatch",
]
