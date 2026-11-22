import asyncio
import json
from types import SimpleNamespace

import pytest
import redis
from aiokafka.structs import TopicPartition

from app.messaging.match_saga_runtime import (
    MATCH_OUTCOME_DLQ_TOPIC,
    MatchSagaRuntime,
    TransientOutcomeFailure,
)


class RecordingProducer:
    def __init__(self, events, fail_count=0):
        self.events = events
        self.fail_count = fail_count

    async def send_and_wait(self, topic, **kwargs):
        if self.fail_count:
            self.fail_count -= 1
            raise OSError("broker unavailable")
        self.events.append(("publish", topic, kwargs))


class RecordingConsumer:
    def __init__(self, events, fail_count=0):
        self.events = events
        self.fail_count = fail_count

    async def commit(self, offsets):
        if self.fail_count:
            self.fail_count -= 1
            raise OSError("coordinator unavailable")
        self.events.append(("commit", offsets))

    def pause(self, _partition):
        return None

    def resume(self, _partition):
        return None


class OutcomeRepository:
    def __init__(self, result="confirmed", fail_count=0):
        self.result = result
        self.fail_count = fail_count
        self.calls = []
        self.effects = 0
        self.applied = set()

    def apply_saga_outcome(self, match_id, correlation_id, status, results):
        self.calls.append((match_id, correlation_id, status, results))
        if self.fail_count:
            self.fail_count -= 1
            raise redis.ConnectionError("redis unavailable")
        if self.result == "confirmed":
            event_key = (match_id, correlation_id)
            if event_key in self.applied:
                return "duplicate"
            self.applied.add(event_key)
            self.effects += 1
        return self.result

    def get_match(self, match_id):
        return {"mode": "VERSUS"}


def kafka_record(value, *, offset=4, partition=0, key=b"match-1"):
    return SimpleNamespace(
        topic="matches.saga.confirmed.v1",
        partition=partition,
        offset=offset,
        key=key,
        value=value,
    )


def valid_outcome():
    return json.dumps({
        "eventId": "event-1",
        "correlationId": "corr-1",
        "payload": {
            "matchId": "match-1",
            "status": "CONFIRMED",
            "results": [{"userId": "alice", "won": True, "xpDelta": 10}],
        },
    }).encode()


def runtime_for(repository=None, *, producer=None, consumer=None, monkeypatch=None):
    runtime = MatchSagaRuntime(repository or OutcomeRepository())
    events = []
    runtime._producer = producer or RecordingProducer(events)
    runtime._consumer = consumer or RecordingConsumer(events)
    runtime._test_events = events
    if monkeypatch:
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return runtime, events


async def _no_sleep(_seconds):
    return None


def test_confirmed_outcome_applies_then_commits():
    repository = OutcomeRepository()
    runtime, events = runtime_for(repository)

    asyncio.run(runtime._process_outcome(kafka_record(valid_outcome())))

    assert repository.effects == 1
    assert [event[0] for event in events] == ["commit"]
    assert next(iter(events[0][1].values())) == 5


def test_duplicate_outcome_does_not_repeat_effects_and_commits():
    repository = OutcomeRepository(result="duplicate")
    runtime, events = runtime_for(repository)

    asyncio.run(runtime._process_outcome(kafka_record(valid_outcome())))

    assert repository.effects == 0
    assert [event[0] for event in events] == ["commit"]


@pytest.mark.parametrize("value", [b"{", None, b"[]"])
def test_malformed_event_is_dlq_acked_before_original_offset_commit(value):
    runtime, events = runtime_for()

    asyncio.run(runtime._process_outcome(kafka_record(value)))

    assert [event[0] for event in events] == ["publish", "commit"]
    publish = events[0]
    assert publish[1] == MATCH_OUTCOME_DLQ_TOPIC
    assert publish[2]["value"] is value
    assert publish[2]["key"] == b"match-1"
    headers = dict(publish[2]["headers"])
    assert headers["original-topic"] == b"matches.saga.confirmed.v1"
    assert headers["original-partition"] == b"0"
    assert headers["original-offset"] == b"4"


@pytest.mark.parametrize(
    "payload",
    [
        {"matchId": "match-1", "status": "CONFIRMED"},
        {"matchId": "match-1", "status": [], "results": []},
        {"matchId": "match-1", "status": "CONFIRMED", "results": [None]},
        {"matchId": "match-1", "status": "CONFIRMED", "results": [{"userId": "?", "won": True, "xpDelta": 1}]},
    ],
    ids=[
        "missing-results",
        "unhashable-status",
        "invalid-result-shape",
        "unknown-participant-from-repository",
    ],
)
def test_incomplete_or_invalid_event_is_traced_in_dlq(payload):
    events = []
    repository = OutcomeRepository(result="invalid")
    runtime, _ = runtime_for(repository)
    runtime._producer = RecordingProducer(events)
    runtime._consumer = RecordingConsumer(events)
    value = json.dumps({"correlationId": "corr-1", "payload": payload}).encode()

    asyncio.run(runtime._process_outcome(kafka_record(value)))

    assert [event[0] for event in events] == ["publish", "commit"]
    assert events[0][2]["value"] == value


@pytest.mark.parametrize("result", ["stale", "conflict", "not_found"])
def test_business_outcomes_use_documented_discard_or_dlq_policy(result):
    runtime, events = runtime_for(OutcomeRepository(result=result))

    asyncio.run(runtime._process_outcome(kafka_record(valid_outcome())))

    if result == "stale":
        assert [event[0] for event in events] == ["commit"]
    else:
        assert [event[0] for event in events] == ["publish", "commit"]
        assert dict(events[0][2]["headers"])["error-type"] == f"business_{result}".encode()


def test_redis_retry_exhaustion_never_commits_and_keeps_error_classified(monkeypatch):
    repository = OutcomeRepository(fail_count=3)
    runtime, events = runtime_for(repository, monkeypatch=monkeypatch)
    runtime._retry_max_attempts = 3
    runtime._retry_backoff_seconds = 0

    with pytest.raises(TransientOutcomeFailure):
        asyncio.run(runtime._process_outcome_with_retries(kafka_record(valid_outcome())))

    assert len(repository.calls) == 3
    assert events == []


def test_redis_recovery_applies_once_and_commits_after_retry(monkeypatch):
    repository = OutcomeRepository(fail_count=2)
    runtime, events = runtime_for(repository, monkeypatch=monkeypatch)
    runtime._retry_max_attempts = 3
    runtime._retry_backoff_seconds = 0

    asyncio.run(runtime._process_outcome_with_retries(kafka_record(valid_outcome())))

    assert len(repository.calls) == 3
    assert repository.effects == 1
    assert [event[0] for event in events] == ["commit"]


def test_dlq_failure_does_not_commit_original_offset(monkeypatch):
    events = []
    producer = RecordingProducer(events, fail_count=1)
    runtime, _ = runtime_for(producer=producer, monkeypatch=monkeypatch)
    runtime._retry_max_attempts = 1

    with pytest.raises(TransientOutcomeFailure):
        asyncio.run(runtime._process_outcome_with_retries(kafka_record(b"{")))

    assert events == []


def test_offset_commit_failure_retries_idempotently(monkeypatch):
    repository = OutcomeRepository()
    events = []
    consumer = RecordingConsumer(events, fail_count=1)
    runtime, _ = runtime_for(repository, consumer=consumer, monkeypatch=monkeypatch)
    runtime._retry_max_attempts = 2
    runtime._retry_backoff_seconds = 0

    asyncio.run(runtime._process_outcome_with_retries(kafka_record(valid_outcome())))

    assert repository.effects == 1
    assert len(repository.calls) == 1
    assert [event[0] for event in events] == ["commit"]

    # Kafka can redeliver after a process interruption even though the business
    # effect was already applied. The fake models Redis' applied -> duplicate contract.
    asyncio.run(runtime._process_outcome(kafka_record(valid_outcome())))
    assert repository.effects == 1
    assert len(repository.calls) == 2
    assert [event[0] for event in events] == ["commit", "commit"]


def test_unknown_repository_result_is_programming_error_and_not_committed():
    runtime, events = runtime_for(OutcomeRepository(result="future-value"))

    with pytest.raises(RuntimeError, match="Unknown apply_saga_outcome result"):
        asyncio.run(runtime._process_outcome(kafka_record(valid_outcome())))

    assert events == []


def test_partition_worker_continues_other_partition_while_first_is_held(monkeypatch):
    events = []

    class Consumer(RecordingConsumer):
        def __init__(self):
            super().__init__(events)
            self.paused = set()
        def pause(self, partition):
            self.paused.add(partition)
        def resume(self, partition):
            self.paused.discard(partition)

    runtime, _ = runtime_for(consumer=Consumer())
    runtime._retry_backoff_seconds = 0.02
    runtime._retry_backoff_max_seconds = 0.02
    runtime._stopping = False
    partition0 = TopicPartition("matches.saga.confirmed.v1", 0)
    partition1 = TopicPartition("matches.saga.confirmed.v1", 1)
    # The first partition's application keeps failing, while the second is healthy.
    class PerPartitionRepository(OutcomeRepository):
        def apply_saga_outcome(self, match_id, correlation_id, status, results):
            if match_id == "match-1":
                raise redis.ConnectionError("partition unavailable")
            return "cancelled"
    runtime._repository = PerPartitionRepository()
    runtime._retry_max_attempts = 1
    queue0, queue1 = asyncio.Queue(), asyncio.Queue()
    queue0.put_nowait(kafka_record(valid_outcome(), partition=0))
    other = json.dumps({"correlationId": "c2", "payload": {"matchId": "m2", "status": "CANCELLED"}}).encode()
    queue1.put_nowait(kafka_record(other, partition=1))

    async def run_workers():
        runtime._consumer.pause(partition0)
        runtime._consumer.pause(partition1)
        task0 = asyncio.create_task(runtime._process_partition(partition0, queue0))
        task1 = asyncio.create_task(runtime._process_partition(partition1, queue1))
        await asyncio.sleep(0.01)
        assert any(event[0] == "commit" and next(iter(event[1])).partition == 1 for event in events)
        assert all(
            next(iter(event[1])).partition == 1
            for event in events
            if event[0] == "commit"
        )
        assert partition0 in runtime._consumer.paused
        runtime._stopping = True
        task0.cancel()
        task1.cancel()
        await asyncio.gather(task0, task1, return_exceptions=True)

    asyncio.run(run_workers())


def test_partition_offsets_remain_ordered_while_first_message_retries():
    events = []
    first_attempted = False

    class OrderedRepository(OutcomeRepository):
        def apply_saga_outcome(self, match_id, correlation_id, status, results):
            nonlocal first_attempted
            self.calls.append((match_id, correlation_id, status, results))
            if not first_attempted:
                first_attempted = True
                raise redis.ConnectionError("temporary failure")
            return "duplicate"

    runtime = MatchSagaRuntime(OrderedRepository())
    runtime._producer = RecordingProducer(events)
    runtime._consumer = RecordingConsumer(events)
    runtime._retry_max_attempts = 1
    runtime._retry_backoff_seconds = 0.05
    runtime._retry_backoff_max_seconds = 0.05
    partition = TopicPartition("matches.saga.confirmed.v1", 0)
    queue = asyncio.Queue()
    queue.put_nowait(kafka_record(valid_outcome(), offset=4))
    queue.put_nowait(kafka_record(valid_outcome(), offset=5))

    async def run_worker():
        worker = asyncio.create_task(runtime._process_partition(partition, queue))
        while not first_attempted:
            await asyncio.sleep(0.005)
        assert events == []
        await asyncio.wait_for(worker, timeout=2)

    asyncio.run(run_worker())

    committed_offsets = [next(iter(event[1].values())) for event in events]
    assert committed_offsets == [5, 6]


def test_rebalance_revocation_cancels_worker_and_discards_uncommitted_queue():
    runtime, events = runtime_for()
    partition = TopicPartition("matches.saga.confirmed.v1", 0)
    queue = asyncio.Queue()
    queue.put_nowait(kafka_record(valid_outcome()))
    runtime._partition_queues[partition] = queue

    async def run_revocation():
        blocker = asyncio.Event()
        worker = asyncio.create_task(blocker.wait())
        runtime._partition_workers[partition] = worker
        await runtime._cancel_revoked_partitions({partition})
        assert worker.cancelled()
        assert partition not in runtime._partition_queues
        assert partition not in runtime._partition_workers
        assert events == []

    asyncio.run(run_revocation())
