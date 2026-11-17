import pytest

from app.application.errors import (
    IncorrectSolution,
    MatchUnavailable,
    ServiceIntegrationError,
)
from app.application.matches import MatchService
from app.application.ports import AppliedUserResult


class FakeStore:
    def __init__(self):
        self.match = {
            "matchId": "m-1",
            "caseId": "case-1",
            "startedAt": 100,
            "mode": "VERSUS",
            "status": "IN_PROGRESS",
            "player1": "user-1",
            "player2": "user-2",
        }
        self.events = []
        self.history = []
        self.scores = []

    def get_match(self, match_id):
        self.events.append(("get", match_id))
        return dict(self.match) if self.match else None

    def complete_match(self, match_id, expected_status, match_data):
        self.events.append(("complete", match_id))
        if not self.match or self.match["status"] != expected_status:
            return False
        self.match = dict(match_data)
        return True

    def increment_score(self, user_id, delta):
        self.scores.append((user_id, delta))

    def save_user_history(self, user_id, entry):
        self.history.append((user_id, dict(entry)))


class FakeCases:
    def __init__(self, valid=True, error=None):
        self.valid = valid
        self.error = error
        self.calls = []

    def validate(self, case_id, positions):
        self.calls.append((case_id, positions))
        if self.error:
            raise self.error
        return self.valid


class FakeUsers:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def register(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return AppliedUserResult(xp_delta=args[-1], first_win_rewarded=False)


class FixedClock:
    def now_seconds(self):
        return 140


class FixedIds:
    def new_id(self):
        return "new-match"


def build_service(valid=True, cases_error=None, users_error=None):
    store = FakeStore()
    cases = FakeCases(valid=valid, error=cases_error)
    users = FakeUsers(error=users_error)
    service = MatchService(store, cases, users, FixedClock(), FixedIds())
    return service, store, cases, users


def test_versus_validates_against_cases_then_records_both_players_once():
    service, store, cases, users = build_service()
    positions = [{"characterId": "S1", "x": 0, "y": 0}]

    result = service.submit_versus_solution("m-1", "user-1", positions)

    assert result == {"winnerId": "user-1", "matchId": "m-1", "xpDelta": 100}
    assert cases.calls == [("case-1", positions)]
    assert [call[0] for call in users.calls] == ["user-1", "user-2"]
    assert store.match["status"] == "FINISHED"
    assert store.scores == [("user-1", 100)]
    assert [entry[1]["result"] for entry in store.history] == ["WON", "LOST"]

    with pytest.raises(MatchUnavailable):
        service.submit_versus_solution("m-1", "user-1", positions)

    assert len(cases.calls) == 1
    assert len(users.calls) == 2


def test_incorrect_versus_solution_does_not_finish_or_record_results():
    service, store, cases, users = build_service(valid=False)

    with pytest.raises(IncorrectSolution):
        service.submit_versus_solution("m-1", "user-1", [])

    assert len(cases.calls) == 1
    assert users.calls == []
    assert store.match["status"] == "IN_PROGRESS"


def test_cases_service_failure_leaves_versus_match_open():
    failure = ServiceIntegrationError("cases unavailable")
    service, store, cases, users = build_service(cases_error=failure)

    with pytest.raises(ServiceIntegrationError) as raised:
        service.submit_versus_solution("m-1", "user-1", [])

    assert raised.value is failure
    assert len(cases.calls) == 1
    assert users.calls == []
    assert store.match["status"] == "IN_PROGRESS"


def test_users_service_failure_is_propagated_after_versus_match_is_closed():
    failure = ServiceIntegrationError("users unavailable")
    service, store, cases, users = build_service(users_error=failure)

    with pytest.raises(ServiceIntegrationError) as raised:
        service.submit_versus_solution("m-1", "user-1", [])

    assert raised.value is failure
    assert len(cases.calls) == 1
    assert len(users.calls) == 1
    assert store.match["status"] == "FINISHED"
    assert store.scores == []
    assert store.history == []
