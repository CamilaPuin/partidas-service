from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from app.application.errors import MatchNotFound, MatchUnavailable, OwnMatch
from app.application.ports import AppliedUserResult
from app.application.timed_matches import CreateTimedMatch, FinishTimedMatch


class FixedClock:
    def __init__(self, now):
        self.now = now

    def now_seconds(self):
        return self.now


class FixedIds:
    def new_id(self):
        return "generated-match-id"


class FakeStore:
    def __init__(self, match=None, complete_result=True):
        self.match = match
        self.complete_result = complete_result
        self.events = []
        self.score_updates = []
        self.history = []
        self.completed_data = None
        self.outbox = []
        self._lock = Lock()

    def get_match(self, match_id):
        self.events.append(("get", match_id))
        return dict(self.match) if self.match is not None else None

    def save_match(self, match_id, match_data):
        self.events.append(("save", match_id))
        self.match = dict(match_data)

    def complete_match(self, match_id, expected_status, match_data):
        self.events.append(("complete", match_id))
        with self._lock:
            if not self.complete_result or not self.match:
                return False
            if self.match.get("status") != expected_status:
                return False
            self.completed_data = dict(match_data)
            self.match = dict(match_data)
            if match_data.get("matchFinishedEvent"):
                self.outbox.append(match_data["matchFinishedEvent"])
            return True

    def increment_score(self, user_id, delta):
        self.events.append(("score", user_id, delta))
        self.score_updates.append((user_id, delta))

    def save_user_history(self, user_id, history_entry):
        self.events.append(("history", user_id))
        self.history.append((user_id, dict(history_entry)))


class FakeCases:
    def __init__(self, valid=True, events=None, barrier=None, error=None):
        self.valid = valid
        self.events = events if events is not None else []
        self.barrier = barrier
        self.error = error
        self.calls = []

    def validate(self, case_id, positions):
        self.events.append(("validate", case_id))
        self.calls.append((case_id, positions))
        if self.barrier:
            self.barrier.wait(timeout=5)
        if self.error:
            raise self.error
        return self.valid


class FakeUsers:
    def __init__(self, events=None, error=None, applied_xp_delta=None):
        self.events = events if events is not None else []
        self.error = error
        self.applied_xp_delta = applied_xp_delta
        self.calls = []

    def register(self, *args):
        self.events.append(("register",) + args)
        self.calls.append(args)
        if self.error:
            raise self.error
        return AppliedUserResult(
            xp_delta=args[-1] if self.applied_xp_delta is None else self.applied_xp_delta,
            first_win_rewarded=False,
        )


def match_data(status="IN_PROGRESS"):
    return {
        "matchId": "m-1",
        "caseId": "case-1",
        "startedAt": 100,
        "deadline": 160,
        "mode": "TIMED",
        "status": status,
        "player1": "user-1",
    }


def test_create_timed_match_persists_current_shape_and_returns_response_fields():
    store = FakeStore()
    use_case = CreateTimedMatch(store, FixedClock(100), FixedIds())

    response = use_case.execute("case-1", 60, "user-1")

    assert response == {
        "matchId": "generated-match-id",
        "caseId": "case-1",
        "startedAt": 100,
        "deadline": 160,
    }
    assert store.match == {
        **response,
        "mode": "TIMED",
        "status": "IN_PROGRESS",
        "player1": "user-1",
    }


def test_finish_winning_match_publishes_durable_match_finished_event():
    store = FakeStore(match_data())
    events = store.events
    cases = FakeCases(events=events)
    users = FakeUsers(events=events)
    use_case = FinishTimedMatch(store, cases, users, FixedClock(150))
    positions = [{"characterId": "c1", "x": 0, "y": 0}]

    response = use_case.execute("m-1", "user-1", positions)

    assert response == {
        "won": True, "timeSeconds": 50, "bonus": 20, "xpDelta": 70,
        "sagaStatus": "PENDING",
    }
    assert cases.calls == [("case-1", positions)]
    assert users.calls == []
    assert [event[0] for event in events] == ["get", "validate", "complete"]
    assert store.completed_data["status"] == "FINISHED"
    assert store.completed_data["sagaStatus"] == "PENDING"
    assert store.completed_data["finalResult"]["won"] is True
    assert store.outbox[0]["payload"]["results"][0] == {
        "userId": "user-1", "won": True, "xpDelta": 70, "difficulty": None,
    }
    assert store.history == []


def test_duplicate_timed_finish_is_rejected_without_revalidating_or_reapplying_result():
    store = FakeStore(match_data())
    cases = FakeCases(events=store.events)
    users = FakeUsers(events=store.events)
    use_case = FinishTimedMatch(store, cases, users, FixedClock(150))

    use_case.execute("m-1", "user-1", [])
    with pytest.raises(MatchUnavailable):
        use_case.execute("m-1", "user-1", [])

    assert len(cases.calls) == 1
    assert len(users.calls) == 0


def test_finish_allows_correct_solution_when_now_equals_deadline():
    store = FakeStore(match_data())
    use_case = FinishTimedMatch(store, FakeCases(), FakeUsers(), FixedClock(160))

    response = use_case.execute("m-1", "user-1", [])

    assert response == {
        "won": True, "timeSeconds": 60, "bonus": 0, "xpDelta": 50,
        "sagaStatus": "PENDING",
    }


def test_incorrect_or_expired_solution_registers_loss_without_bonus():
    for now, valid in ((150, False), (161, True)):
        store = FakeStore(match_data())
        cases = FakeCases(valid=valid)
        users = FakeUsers()
        response = FinishTimedMatch(store, cases, users, FixedClock(now)).execute(
            "m-1", "user-1", []
        )
        assert response == {
            "won": False,
            "timeSeconds": now - 100,
            "bonus": 0,
            "xpDelta": -10,
            "sagaStatus": "PENDING",
        }
        assert users.calls == []


def test_deadline_expiry_still_validates_case_before_result_transition():
    store = FakeStore(match_data())
    cases = FakeCases(valid=True, events=store.events)
    use_case = FinishTimedMatch(store, cases, FakeUsers(events=store.events), FixedClock(161))

    use_case.execute("m-1", "user-1", [])

    assert [event[0] for event in store.events[:3]] == ["get", "validate", "complete"]
    assert len(cases.calls) == 1


@pytest.mark.parametrize(
    ("stored_match", "user_id", "error"),
    [
        (None, "user-1", MatchNotFound),
        (match_data(), "other-user", OwnMatch),
        (match_data(status="FINISHED"), "user-1", MatchUnavailable),
    ],
)
def test_preconditions_reject_without_calling_cases(stored_match, user_id, error):
    store = FakeStore(stored_match)
    cases = FakeCases()
    use_case = FinishTimedMatch(store, cases, FakeUsers(), FixedClock(120))

    with pytest.raises(error):
        use_case.execute("m-1", user_id, [])

    assert cases.calls == []
    assert not any(event[0] == "complete" for event in store.events)


def test_case_service_error_leaves_match_open_and_propagates_same_exception():
    error = RuntimeError("cases unavailable")
    store = FakeStore(match_data())
    use_case = FinishTimedMatch(store, FakeCases(error=error), FakeUsers(), FixedClock(120))

    with pytest.raises(RuntimeError) as raised:
        use_case.execute("m-1", "user-1", [])

    assert raised.value is error
    assert store.match["status"] == "IN_PROGRESS"
    assert not any(event[0] == "complete" for event in store.events)


def test_failed_atomic_transition_rejects_without_recording_user_result():
    store = FakeStore(match_data(), complete_result=False)
    users = FakeUsers()
    use_case = FinishTimedMatch(store, FakeCases(), users, FixedClock(120))

    with pytest.raises(MatchUnavailable):
        use_case.execute("m-1", "user-1", [])

    assert users.calls == []
    assert store.score_updates == []
    assert store.history == []


def test_users_failure_is_deferred_to_the_durable_event_retry_path():
    store = FakeStore(match_data())
    error = RuntimeError("users unavailable")
    use_case = FinishTimedMatch(
        store,
        FakeCases(),
        FakeUsers(error=error),
        FixedClock(120),
    )

    response = use_case.execute("m-1", "user-1", [])
    assert response["sagaStatus"] == "PENDING"
    assert store.match["status"] == "FINISHED"
    assert store.outbox
    assert store.score_updates == []
    assert store.history == []


def test_event_uses_server_calculated_xp_and_defers_ranking_until_confirmation():
    store = FakeStore(match_data())
    users = FakeUsers(applied_xp_delta=13)
    use_case = FinishTimedMatch(store, FakeCases(), users, FixedClock(150))

    response = use_case.execute("m-1", "user-1", [])

    assert response == {
        "won": True, "timeSeconds": 50, "bonus": 20, "xpDelta": 70,
        "sagaStatus": "PENDING",
    }
    assert store.outbox[0]["payload"]["results"][0]["xpDelta"] == 70
    assert store.score_updates == []


def test_simultaneous_finish_requests_close_the_match_only_once():
    barrier = Barrier(2)
    store = FakeStore(match_data())
    events = store.events
    cases = FakeCases(events=events, barrier=barrier)
    users = FakeUsers(events=events)
    use_case = FinishTimedMatch(store, cases, users, FixedClock(120))

    def finish():
        try:
            return use_case.execute("m-1", "user-1", [])
        except MatchUnavailable:
            return "unavailable"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: finish(), range(2)))

    assert results.count("unavailable") == 1
    assert len(users.calls) == 0
    assert len(store.outbox) == 1
    assert len(store.score_updates) == 0
    assert len(store.history) == 0
