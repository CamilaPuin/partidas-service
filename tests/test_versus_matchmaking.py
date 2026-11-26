from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import RLock, Barrier

import pytest

from app.application.errors import NoEligibleCase, OwnMatch
from app.application.matches import MatchService


CASES = [
    {"id": "easy-a", "difficulty": "EASY", "requiredLevel": 1},
    {"id": "easy-b", "difficulty": "EASY", "requiredLevel": 1},
    {"id": "medium-a", "difficulty": "MEDIUM", "requiredLevel": 5},
    {"id": "hard-a", "difficulty": "HARD", "requiredLevel": 10},
]


class FakeStore:
    def __init__(self):
        self.matches = {}
        self.open_matches = set()
        self.published_progress = []
        self._matchmaking_lock = RLock()

    @contextmanager
    def versus_matchmaking_lock(self):
        with self._matchmaking_lock:
            yield

    def get_match(self, match_id):
        match = self.matches.get(match_id)
        return dict(match) if match is not None else None

    def save_match(self, match_id, match_data):
        self.matches[match_id] = dict(match_data)

    def complete_match(self, match_id, expected_status, match_data):
        current = self.matches.get(match_id)
        if current is None or current["status"] != expected_status:
            return False
        self.matches[match_id] = dict(match_data)
        return True

    def add_open_match(self, match_id):
        self.open_matches.add(match_id)

    def remove_open_match(self, match_id):
        self.open_matches.discard(match_id)

    def list_open_match_ids(self):
        return set(self.open_matches)

    def save_progress(self, *_args):
        pass

    def publish_progress(self, match_id, user_id, progress):
        self.published_progress.append((match_id, user_id, progress))

    def get_progress(self, *_args):
        return 0.0


class FakeCases:
    def list_case_summaries(self):
        return list(CASES)


class FakeUsers:
    def __init__(self, levels=None, solved=None):
        self.levels = levels or {"host": 10, "guest": 10}
        self.solved = solved or {}

    def get_progression(self, user_id):
        return {"level": self.levels[user_id]}

    def get_solved_case_ids(self, user_id):
        return self.solved.get(user_id, [])

    def get_display_name(self, user_id):
        return "Host Detective" if user_id == "host" else "Guest Detective"


class FixedClock:
    def now_seconds(self):
        return 1234


class FixedIds:
    def new_id(self):
        return "match-1"


def build_service(users=None):
    store = FakeStore()
    service = MatchService(store, FakeCases(), users or FakeUsers(), FixedClock(), FixedIds())
    return service, store


def test_versus_assigns_case_only_after_both_players_join_and_excludes_both_histories():
    users = FakeUsers(
        levels={"host": 5, "guest": 10},
        solved={"host": ["easy-a"], "guest": ["easy-b", "hard-a"]},
    )
    service, store = build_service(users)

    created = service.create_versus_match("host")

    assert created["caseId"] is None
    assert created["status"] == "WAITING"
    assert created["player1Name"] == "Host Detective"
    assert store.open_matches == {"match-1"}

    joined = service.join_versus_match("match-1", "guest")

    assert joined["caseId"] == "medium-a"
    assert joined["status"] == "IN_PROGRESS"
    assert store.matches["match-1"]["caseId"] == "medium-a"
    assert store.matches["match-1"]["player2"] == "guest"
    assert joined["player2Name"] == "Guest Detective"
    assert store.open_matches == set()


def test_versus_does_not_start_when_players_have_no_common_unresolved_case():
    users = FakeUsers(solved={"host": ["easy-a", "easy-b"]})
    service, store = build_service(users)
    service.create_versus_match("host")
    users.solved["guest"] = ["medium-a", "hard-a"]

    with pytest.raises(NoEligibleCase):
        service.join_versus_match("match-1", "guest")

    assert store.matches["match-1"]["status"] == "WAITING"
    assert store.matches["match-1"]["player2"] is None
    assert store.open_matches == {"match-1"}


def test_versus_cannot_create_room_without_any_unresolved_case_at_current_level():
    users = FakeUsers(levels={"host": 1}, solved={"host": ["easy-a", "easy-b"]})
    service, store = build_service(users)

    with pytest.raises(NoEligibleCase):
        service.create_versus_match("host")

    assert store.matches == {}
    assert store.open_matches == set()


def test_simultaneous_queue_requests_match_into_one_room():
    service, store = build_service()
    start = Barrier(2)

    def queue(user_id):
        start.wait()
        return service.create_versus_match(user_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        host_future = pool.submit(queue, "host")
        guest_future = pool.submit(queue, "guest")
        host = host_future.result()
        guest = guest_future.result()

    assert guest["matchId"] == host["matchId"]
    final_match = store.matches[host["matchId"]]
    assert final_match["status"] == "IN_PROGRESS"
    assert {final_match["player1"], final_match["player2"]} == {"host", "guest"}
    assert final_match["caseId"] in {case["id"] for case in CASES}
    assert store.open_matches == set()
    assert len(store.matches) == 1


def test_repeated_create_request_reuses_players_waiting_room():
    service, store = build_service()
    first = service.create_versus_match("host")

    repeated = service.create_versus_match("host")

    assert repeated == first
    assert store.open_matches == {first["matchId"]}
    assert len(store.matches) == 1


def test_versus_progress_includes_server_assigned_case():
    users = FakeUsers()
    service, store = build_service(users)
    service.create_versus_match("host")
    service.join_versus_match("match-1", "guest")

    progress = service.get_versus_progress("match-1", "host")

    assert progress["caseId"] == store.matches["match-1"]["caseId"]
    assert progress["me"] == 0.0
    assert progress["rival"] == 0.0
    assert progress["opponentName"] == "Guest Detective"
    assert progress["startedAt"] == 1234


def test_versus_progress_is_persisted_and_published_only_by_active_participants():
    service, store = build_service()
    service.create_versus_match("host")
    service.join_versus_match("match-1", "guest")

    assert service.report_progress("match-1", "host", 0.4) == {"ok": True, "progress": 0.4}
    assert store.published_progress == [("match-1", "host", 0.4)]

    with pytest.raises(OwnMatch):
        service.report_progress("match-1", "stranger", 0.8)
    assert store.published_progress == [("match-1", "host", 0.4)]


def test_creator_can_cancel_waiting_room_and_it_is_removed_from_open_matches():
    service, store = build_service()
    service.create_versus_match("host")

    assert service.cancel_versus_match("match-1", "host") == {
        "matchId": "match-1",
        "status": "CANCELLED",
    }
    assert store.matches["match-1"]["status"] == "CANCELLED"
    assert store.open_matches == set()


def test_only_creator_can_cancel_waiting_room():
    service, store = build_service()
    service.create_versus_match("host")

    with pytest.raises(OwnMatch):
        service.cancel_versus_match("match-1", "guest")

    assert store.matches["match-1"]["status"] == "WAITING"
    assert store.open_matches == {"match-1"}
