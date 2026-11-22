import asyncio

from app.messaging.saga_reconciler import SagaReconciler


class FakeRedis:
    def __init__(self):
        self.values = {"matches:saga:reconcile:match-1": {"attempts": "2"}}
        self.pending = 1
        self.rebuild_calls = 0

    def hget(self, key, field):
        return self.values.get(key, {}).get(field)

    def zcard(self, _key):
        return self.pending


class FakeRepository:
    def __init__(self):
        self.client = FakeRedis()
        self.claim_calls = []
        self.reemit_calls = []

    def rebuild_pending_saga_index(self):
        self.client.rebuild_calls += 1
        return 1

    def claim_pending_sagas(self, **kwargs):
        self.claim_calls.append(kwargs)
        return ([("match-1", "lease-token", {"eventId": "original-event"})], [("match-1", "leased")])

    def reemit_pending_saga(self, match_id, token, *, next_attempt_at):
        self.reemit_calls.append((match_id, token, next_attempt_at))
        return "requeued"

    def oldest_pending_saga_finished_at(self):
        return 1234


def test_reconciler_uses_bounded_claim_and_exponential_backoff(monkeypatch):
    monkeypatch.setenv("MATCH_SAGA_RECONCILIATION_BACKOFF_SECONDS", "10")
    monkeypatch.setenv("MATCH_SAGA_RECONCILIATION_BACKOFF_MAX_SECONDS", "100")
    repository = FakeRepository()
    reconciler = SagaReconciler(repository)
    reconciler._reconcile_once()

    assert repository.claim_calls[0]["limit"] == 50
    assert repository.claim_calls[0]["max_attempts"] == 5
    assert repository.reemit_calls[0][0:2] == ("match-1", "lease-token")
    # Attempt 2 applies 10 * 2 ** (2 - 1) seconds from the call's clock.
    import app.messaging.saga_reconciler as module
    now = int(module.datetime.now(module.timezone.utc).timestamp())
    assert repository.reemit_calls[0][2] in {now + 20, now + 21}


def test_disabled_reconciler_does_not_touch_repository(monkeypatch):
    monkeypatch.setenv("MATCH_SAGA_RECONCILIATION_ENABLED", "false")
    repository = FakeRepository()
    reconciler = SagaReconciler(repository)
    asyncio.run(reconciler.start())
    assert repository.claim_calls == []
    assert repository.reemit_calls == []
    assert repository.client.rebuild_calls == 0
