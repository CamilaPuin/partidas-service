import asyncio
import json
import os
import uuid

import redis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError
from aiokafka.structs import TopicPartition

from app.messaging.match_saga_runtime import (
    MATCH_OUTCOME_DLQ_TOPIC,
    MATCH_OUTCOME_TOPIC,
    MatchSagaRuntime,
)
from app.repositories.matches import MatchRepository


BOOTSTRAP = os.getenv("H04_KAFKA_BOOTSTRAP_SERVERS", "")
REDIS_HOST = os.getenv("H04_TEST_REDIS_HOST", "")


async def _wait_for(predicate, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Timed out waiting for isolated Kafka/Redis state")


async def _wait_for_committed_offset(admin, group_id, partition, expected, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    topic_partition = TopicPartition(MATCH_OUTCOME_TOPIC, partition)
    observed = None
    while asyncio.get_running_loop().time() < deadline:
        offsets = await admin.list_consumer_group_offsets(
            group_id, partitions=[topic_partition]
        )
        committed = offsets.get(topic_partition)
        observed = committed.offset if committed is not None else None
        if observed is not None and observed >= expected:
            return observed
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Kafka group {group_id} did not commit {MATCH_OUTCOME_TOPIC}[{partition}] "
        f"through offset {expected}; last observed={observed}"
    )


def _match_fixture(suffix):
    match_id = f"h04-04-{suffix}"
    correlation_id = f"corr-{suffix}"
    event_id = f"event-{suffix}"
    player1, player2 = f"winner-{suffix}", f"loser-{suffix}"
    terminal_results = [
        {"userId": player1, "won": True, "xpDelta": 100, "difficulty": None},
        {"userId": player2, "won": False, "xpDelta": 0, "difficulty": None},
    ]
    match = {
        "matchId": match_id,
        "mode": "VERSUS",
        "status": "FINISHED",
        "caseId": "case-h04-04",
        "player1": player1,
        "player2": player2,
        "winnerId": player1,
        "sagaStatus": "PENDING",
        "sagaEventId": event_id,
        "correlationId": correlation_id,
        "finishedAt": 1234,
        "matchFinishedEvent": {
            "eventId": event_id,
            "correlationId": correlation_id,
            "payload": {
                "matchId": match_id,
                "mode": "VERSUS",
                "caseId": "case-h04-04",
                "results": terminal_results,
            },
        },
    }
    outcome = {
        "eventId": event_id,
        "correlationId": correlation_id,
        "payload": {
            "matchId": match_id,
            "status": "CONFIRMED",
            "results": [
                {"userId": player1, "won": True, "xpDelta": 100, "firstWinRewarded": False},
                {"userId": player2, "won": False, "xpDelta": 0, "firstWinRewarded": False},
            ],
        },
    }
    return match_id, match, outcome, player1, player2


def test_real_kafka_dlq_order_and_restart_redelivery(monkeypatch):
    if not BOOTSTRAP or not REDIS_HOST:
        raise AssertionError("H04-04 integration requires isolated Kafka and Redis services")

    async def scenario():
        suffix = uuid.uuid4().hex
        group_id = f"h04-04-{suffix}"
        first_id, first_match, first_outcome, first_winner, first_loser = _match_fixture(f"{suffix}-one")
        second_id, second_match, second_outcome, second_winner, second_loser = _match_fixture(f"{suffix}-two")
        monkeypatch.setenv("KAFKA_GROUP_ID_PARTIDAS_SAGA", group_id)
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=int(os.getenv("H04_TEST_REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        redis_client.ping()

        admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP, client_id=f"h04-admin-{suffix}")
        await admin.start()
        try:
            try:
                await admin.create_topics([
                    NewTopic(MATCH_OUTCOME_TOPIC, num_partitions=2, replication_factor=1),
                    NewTopic(MATCH_OUTCOME_DLQ_TOPIC, num_partitions=2, replication_factor=1),
                ])
            except TopicAlreadyExistsError:
                pass

            dlq_consumer = AIOKafkaConsumer(
                MATCH_OUTCOME_DLQ_TOPIC,
                bootstrap_servers=BOOTSTRAP,
                group_id=f"h04-04-dlq-reader-{suffix}",
                enable_auto_commit=False,
                auto_offset_reset="earliest",
            )
            await dlq_consumer.start()
            producer = AIOKafkaProducer(
                bootstrap_servers=BOOTSTRAP,
                client_id=f"h04-04-test-producer-{suffix}",
                acks="all",
                enable_idempotence=True,
            )
            await producer.start()
            runtime = MatchSagaRuntime(MatchRepository(redis_client))
            await runtime.start()
            try:
                await _wait_for(lambda: runtime._consumer is not None and bool(runtime._consumer.assignment()))

                redis_client.set(f"match:{first_id}", json.dumps(first_match))
                key = f"serial-{suffix}".encode()
                malformed = b"{"
                invalid_meta = await producer.send_and_wait(MATCH_OUTCOME_TOPIC, key=key, value=malformed)
                valid_value = json.dumps(first_outcome).encode()
                first_valid_meta = await producer.send_and_wait(MATCH_OUTCOME_TOPIC, key=key, value=valid_value)
                duplicate_meta = await producer.send_and_wait(MATCH_OUTCOME_TOPIC, key=key, value=valid_value)

                dlq_record = await asyncio.wait_for(dlq_consumer.getone(), timeout=20)
                assert dlq_record.key == key
                assert dlq_record.value == malformed
                headers = dict(dlq_record.headers)
                assert headers["original-topic"] == MATCH_OUTCOME_TOPIC.encode()
                assert headers["original-partition"] == str(invalid_meta.partition).encode()
                assert headers["original-offset"] == str(invalid_meta.offset).encode()
                assert headers["error-type"] == b"InvalidOutcome"
                assert headers["error-description"]
                assert headers["failed-at"]
                assert headers["attempts"] == b"1"

                await _wait_for(lambda: json.loads(redis_client.get(f"match:{first_id}")).get("sagaStatus") == "CONFIRMED")
                await _wait_for(lambda: redis_client.llen(f"user:{first_winner}:history") == 1)
                assert redis_client.zscore("ranking:global", first_winner) == 100
                assert redis_client.llen(f"user:{first_loser}:history") == 1
                assert first_valid_meta.partition == duplicate_meta.partition == invalid_meta.partition

                observed_offset = await _wait_for_committed_offset(
                    admin,
                    group_id,
                    invalid_meta.partition,
                    duplicate_meta.offset + 1,
                )
                assert observed_offset >= duplicate_meta.offset + 1

                # Simulate a process interruption after Redis committed the outcome but
                # before Kafka could store the offset. The next runtime must replay it.
                redis_client.set(f"match:{second_id}", json.dumps(second_match))
                commit_attempted = asyncio.Event()
                commit_attempts = 0
                apply_calls = 0
                original_apply = runtime._repository.apply_saga_outcome

                def track_apply(*args, **kwargs):
                    nonlocal apply_calls
                    apply_calls += 1
                    return original_apply(*args, **kwargs)

                runtime._repository.apply_saga_outcome = track_apply

                async def fail_commit(_offsets):
                    nonlocal commit_attempts
                    commit_attempts += 1
                    commit_attempted.set()
                    raise OSError("isolated simulated shutdown before offset commit")

                runtime._consumer.commit = fail_commit
                second_value = json.dumps(second_outcome).encode()
                second_meta = await producer.send_and_wait(
                    MATCH_OUTCOME_TOPIC,
                    key=f"restart-{suffix}".encode(),
                    value=second_value,
                )
                await _wait_for(lambda: json.loads(redis_client.get(f"match:{second_id}")).get("sagaStatus") == "CONFIRMED")
                await asyncio.wait_for(commit_attempted.wait(), timeout=10)
                await _wait_for(lambda: commit_attempts >= 2, timeout=10)
                assert apply_calls == 1
                await runtime.stop()

                restarted_repository = MatchRepository(redis_client)
                replay_results = []
                original_replay_apply = restarted_repository.apply_saga_outcome

                def track_replay(*args, **kwargs):
                    result = original_replay_apply(*args, **kwargs)
                    replay_results.append(result)
                    return result

                restarted_repository.apply_saga_outcome = track_replay
                runtime_after_restart = MatchSagaRuntime(restarted_repository)
                await runtime_after_restart.start()
                try:
                    await _wait_for(
                        lambda: runtime_after_restart._consumer is not None
                        and bool(runtime_after_restart._consumer.assignment())
                    )
                    await _wait_for_committed_offset(
                        admin,
                        group_id,
                        second_meta.partition,
                        second_meta.offset + 1,
                    )
                    assert replay_results == ["duplicate"]
                    assert redis_client.zscore("ranking:global", second_winner) == 100
                    assert redis_client.llen(f"user:{second_winner}:history") == 1
                finally:
                    await runtime_after_restart.stop()
            finally:
                await runtime.stop()
                await producer.stop()
                await dlq_consumer.stop()
                redis_client.delete(
                    f"match:{first_id}", f"match:{second_id}",
                    f"user:{first_winner}:history", f"user:{first_loser}:history",
                    f"user:{second_winner}:history", f"user:{second_loser}:history",
                )
                redis_client.zrem("ranking:global", first_winner, first_loser, second_winner)
                redis_client.close()
        finally:
            await admin.close()

    asyncio.run(scenario())
