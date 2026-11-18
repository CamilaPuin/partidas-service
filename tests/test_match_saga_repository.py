import json

from app.repositories.matches import MatchRepository


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
        "correlationId": "corr-42",
        "finishedAt": 1234,
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
    }
    assert "secret" not in json.dumps(status)
