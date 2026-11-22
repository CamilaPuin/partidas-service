import json
from copy import deepcopy

import pytest

from app.repositories.matches import MatchRepository


def versus_fixture():
    match = {
        "matchId": "versus-1",
        "mode": "VERSUS",
        "status": "FINISHED",
        "caseId": "case-1",
        "player1": "alice",
        "player2": "bob",
        "winnerId": "alice",
        "sagaStatus": "PENDING",
        "sagaEventId": "event-v1",
        "correlationId": "corr-v1",
        "finishedAt": 1234,
        "matchFinishedEvent": {
            "eventId": "event-v1",
            "correlationId": "corr-v1",
            "payload": {
                "matchId": "versus-1",
                "mode": "VERSUS",
                "caseId": "case-1",
                "results": [
                    {"userId": "alice", "won": True, "xpDelta": 100},
                    {"userId": "bob", "won": False, "xpDelta": 0},
                ],
            },
        },
    }
    results = [
        {"userId": "alice", "won": True, "xpDelta": 100, "firstWinRewarded": False},
        {"userId": "bob", "won": False, "xpDelta": 0, "firstWinRewarded": False},
    ]
    return match, results


class ImmediatePipeline:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def watch(self, *_keys):
        return None

    def multi(self):
        return None

    def get(self, key):
        return self.client.values.get(key)

    def set(self, key, value, ex=None):
        self.client.values[key] = value
        return True

    def expire(self, *_args):
        return True

    def zincrby(self, key, amount, member):
        self.client.scores[member] = self.client.scores.get(member, 0) + amount

    def lpush(self, key, value):
        self.client.lists.setdefault(key, []).insert(0, value)

    def execute(self):
        return []


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}
        self.scores = {}

    def pipeline(self):
        return ImmediatePipeline(self)

    def get(self, key):
        return self.values.get(key)

    def eval(self, _script, number_of_keys, *values):
        keys = values[:number_of_keys]
        arguments = values[number_of_keys:]
        match_key = keys[0]
        if match_key not in self.values:
            return "retry"
        if self.values[match_key] != arguments[0]:
            return "retry"
        if number_of_keys > 1:
            if keys[1] in self.values:
                return "redis_type_error"
            if any(key in self.values for key in keys[2::2]):
                return "redis_type_error"
        self.values[match_key] = arguments[1]
        for key_index in range(1, number_of_keys, 2):
            result_index = (key_index - 1) // 2
            argument_index = 3 + result_index * 3
            user_id, xp_delta, history = arguments[argument_index:argument_index + 3]
            self.scores[user_id] = self.scores.get(user_id, 0) + int(xp_delta)
            self.lists.setdefault(keys[key_index + 1], []).insert(0, history)
        return "applied"


def test_match_completion_stores_finished_state_and_outbox_event_together():
    client = FakeRedis()
    client.values["match:match-45"] = json.dumps({"matchId": "match-45", "status": "IN_PROGRESS"})
    repository = MatchRepository(client)
    event = {"eventId": "event-45", "correlationId": "corr-45", "payload": {"matchId": "match-45"}}
    final_match = {
        "matchId": "match-45",
        "status": "FINISHED",
        "sagaStatus": "PENDING",
        "matchFinishedEvent": event,
    }

    assert repository.complete_match("match-45", "IN_PROGRESS", final_match)
    assert json.loads(client.values["match:match-45"])["sagaStatus"] == "PENDING"
    assert json.loads(client.lists["outbox:matches.finished.v1"][0]) == event


def test_confirmation_updates_ranking_and_history_once_and_replays_duplicate_ack():
    client = FakeRedis()
    match = {
        "matchId": "match-42",
        "caseId": "case-9",
        "mode": "VERSUS",
        "player1": "winner",
        "player2": "loser",
        "sagaStatus": "PENDING",
        "status": "FINISHED",
        "correlationId": "corr-42",
        "sagaEventId": "event-42",
        "finishedAt": 1234,
        "matchFinishedEvent": {
            "eventId": "event-42",
            "correlationId": "corr-42",
            "payload": {
                "matchId": "match-42",
                "mode": "VERSUS",
                "caseId": "case-9",
                "results": [
                    {"userId": "winner", "won": True, "xpDelta": 100},
                    {"userId": "loser", "won": False, "xpDelta": 0},
                ],
            },
        },
        "winnerId": "winner",
    }
    client.values["match:match-42"] = json.dumps(match)
    repository = MatchRepository(client)
    applied = [
        {"userId": "winner", "won": True, "xpDelta": 100, "firstWinRewarded": False},
        {"userId": "loser", "won": False, "xpDelta": 0, "firstWinRewarded": False},
    ]

    assert repository.apply_saga_outcome("match-42", "corr-42", "CONFIRMED", applied) == "confirmed"
    assert repository.apply_saga_outcome("match-42", "corr-42", "CONFIRMED", applied) == "duplicate"
    assert client.scores == {"winner": 100, "loser": 0}
    assert len(client.lists["user:winner:history"]) == 1
    assert len(client.lists["user:loser:history"]) == 1
    assert json.loads(client.values["match:match-42"])["sagaStatus"] == "CONFIRMED"


@pytest.mark.parametrize(
    "mutate_match, mutate_results",
    [
        (lambda match: None, lambda results: results.pop(0)),
        (lambda match: None, lambda results: results.pop()),
        (lambda match: None, lambda results: results.clear()),
        (lambda match: None, lambda results: results.append(deepcopy(results[0]))),
        (lambda match: None, lambda results: results.append({"userId": "mallory", "won": False, "xpDelta": 1})),
        (lambda match: None, lambda results: results.append({"userId": "eve", "won": False, "xpDelta": 1})),
        (lambda match: match.update(matchId="different"), lambda results: None),
        (lambda match: match["matchFinishedEvent"]["payload"].update(mode="CLASSIC"), lambda results: None),
        (lambda match: match.update(mode="CLASSIC"), lambda results: None),
        (lambda match: None, lambda results: results[1].update(won=True)),
        (lambda match: None, lambda results: results[1].update(xpDelta="0")),
    ],
    ids=[
        "missing-first", "missing-second", "empty", "duplicate", "unknown",
        "extra", "wrong-match-id", "event-mode-mismatch", "mode-mismatch",
        "contradictory-winner", "invalid-xp",
    ],
)
def test_invalid_versus_confirmation_has_no_side_effects(mutate_match, mutate_results):
    client = FakeRedis()
    match, results = versus_fixture()
    mutate_match(match)
    mutate_results(results)
    raw_match = json.dumps(match)
    client.values["match:versus-1"] = raw_match
    repository = MatchRepository(client)

    assert repository.apply_saga_outcome("versus-1", "corr-v1", "CONFIRMED", results) == "invalid"
    assert client.values["match:versus-1"] == raw_match
    assert client.scores == {}
    assert client.lists == {}


def test_valid_versus_confirmation_after_invalid_confirmation_succeeds():
    client = FakeRedis()
    match, results = versus_fixture()
    client.values["match:versus-1"] = json.dumps(match)
    repository = MatchRepository(client)

    assert repository.apply_saga_outcome("versus-1", "corr-v1", "CONFIRMED", results[:1]) == "invalid"
    assert repository.apply_saga_outcome("versus-1", "corr-v1", "CONFIRMED", results) == "confirmed"
    assert client.scores == {"alice": 100, "bob": 0}
    assert len(client.lists["user:alice:history"]) == 1
    assert len(client.lists["user:bob:history"]) == 1


def test_reordered_confirmation_is_idempotent_but_contradictory_repeat_conflicts():
    client = FakeRedis()
    match, results = versus_fixture()
    client.values["match:versus-1"] = json.dumps(match)
    repository = MatchRepository(client)

    assert repository.apply_saga_outcome("versus-1", "corr-v1", "CONFIRMED", results) == "confirmed"
    before_scores = dict(client.scores)
    before_history = deepcopy(client.lists)
    assert repository.apply_saga_outcome("versus-1", "corr-v1", "CONFIRMED", list(reversed(results))) == "duplicate"

    contradictory = deepcopy(results)
    contradictory[0]["xpDelta"] = 99
    assert repository.apply_saga_outcome("versus-1", "corr-v1", "CONFIRMED", contradictory) == "conflict"
    assert client.scores == before_scores
    assert client.lists == before_history


@pytest.mark.parametrize(
    "mode, terminal_result, outcome_result",
    [
        ("CLASSIC", {"userId": "solo", "won": True, "xpDelta": None}, {"userId": "solo", "won": True, "xpDelta": 125}),
        ("TIMED", {"userId": "solo", "won": False, "xpDelta": -10}, {"userId": "solo", "won": False, "xpDelta": -10}),
    ],
)
def test_classic_and_timed_confirmations_use_stored_terminal_participant(mode, terminal_result, outcome_result):
    client = FakeRedis()
    match = {
        "matchId": f"{mode.lower()}-1", "mode": mode, "status": "FINISHED",
        "player1": "solo", "caseId": "case-1", "sagaStatus": "PENDING",
        "sagaEventId": f"event-{mode}", "correlationId": f"corr-{mode}", "finishedAt": 2000,
        "matchFinishedEvent": {
            "eventId": f"event-{mode}", "correlationId": f"corr-{mode}",
            "payload": {"matchId": f"{mode.lower()}-1", "mode": mode, "caseId": "case-1", "results": [terminal_result]},
        },
    }
    client.values[f"match:{mode.lower()}-1"] = json.dumps(match)
    repository = MatchRepository(client)

    assert repository.apply_saga_outcome(
        f"{mode.lower()}-1", f"corr-{mode}", "CONFIRMED", [outcome_result]
    ) == "confirmed"
    assert len(client.lists["user:solo:history"]) == 1
    assert client.scores == {"solo": outcome_result["xpDelta"]}


def test_redis_key_type_conflict_prevents_partial_confirmation():
    client = FakeRedis()
    match, results = versus_fixture()
    client.values["match:versus-1"] = json.dumps(match)
    client.values["user:bob:history"] = "wrong-type"
    before_match = client.values["match:versus-1"]
    repository = MatchRepository(client)

    assert repository.apply_saga_outcome("versus-1", "corr-v1", "CONFIRMED", results) == "invalid"
    assert client.values["match:versus-1"] == before_match
    assert client.scores == {}
    assert client.lists == {}


def test_cancelled_and_stale_saga_outcomes_do_not_apply_rewards():
    client = FakeRedis()
    client.values["match:match-43"] = json.dumps({
        "matchId": "match-43", "mode": "CLASSIC", "caseId": "case-1",
        "sagaStatus": "PENDING", "correlationId": "corr-current",
    })
    repository = MatchRepository(client)

    assert repository.apply_saga_outcome("match-43", "corr-old", "CONFIRMED", []) == "stale"
    assert repository.apply_saga_outcome("match-43", "corr-current", "CANCELLED", []) == "cancelled"
    assert client.scores == {}
    assert client.lists == {}
    assert json.loads(client.values["match:match-43"])["sagaStatus"] == "CANCELLED"


def test_match_saga_status_does_not_expose_the_outbox_payload():
    client = FakeRedis()
    client.values["match:match-44"] = json.dumps({
        "matchId": "match-44", "sagaStatus": "PENDING", "correlationId": "corr-44",
        "matchFinishedEvent": {"payload": {"secret": "must-not-leak"}},
    })
    status = MatchRepository(client).get_saga_status("match-44")

    assert status == {
        "matchId": "match-44",
        "status": "PENDING",
        "updatedAt": None,
        "correlationId": "corr-44",
        "results": [],
        "caseId": None,
        "winnerId": None,
        "winnerName": None,
        "player1": None,
        "player2": None,
        "player1Name": None,
        "player2Name": None,
        "timeSeconds": None,
        "playerTimes": [],
        "solution": None,
    }
    assert "secret" not in json.dumps(status)
