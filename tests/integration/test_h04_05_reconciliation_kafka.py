import asyncio
import json
import os
import uuid

import pytest
import redis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from app.messaging.match_saga_runtime import MATCH_FINISHED_TOPIC, MatchSagaRuntime
from app.repositories.matches import (
    MATCH_EVENT_OUTBOX,
    MatchRepository,
    PENDING_SAGAS,
    SAGA_RECONCILIATION_DUE,
)


@pytest.fixture
def isolated_redis_case():
    host = os.getenv("H04_TEST_REDIS_HOST")
    if not host:
        pytest.skip("requires isolated H04-05 Redis service")
    client = redis.Redis(
        host=host,
        port=int(os.getenv("H04_TEST_REDIS_PORT", "6379")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )
    client.ping()
    suffix = uuid.uuid4().hex
    match_id = f"h04-kafka-reconcile-{suffix}"
    event = {
        "eventId": f"event-{suffix}",
        "correlationId": f"corr-{suffix}",
        "occurredAt": "2026-01-01T00:00:00Z",
        "payload": {
            "matchId": match_id,
            "mode": "VERSUS",
            "caseId": "case-test",
            "timeSeconds": 10,
            "results": [
                {"userId": f"a-{suffix}", "won": True, "xpDelta": 100},
                {"userId": f"b-{suffix}", "won": False, "xpDelta": 0},
            ],
        },
    }
    match = {
        "matchId": match_id,
        "mode": "VERSUS",
        "status": "FINISHED",
        "caseId": "case-test",
        "player1": f"a-{suffix}",
        "player2": f"b-{suffix}",
        "sagaStatus": "PENDING",
        "sagaEventId": event["eventId"],
        "correlationId": event["correlationId"],
        "finishedAt": 1234,
        "matchFinishedEvent": event,
    }
    client.set(f"match:{match_id}", json.dumps(match))
    yield {"client": client, "match_id": match_id}
    client.delete(
        f"match:{match_id}",
        f"matches:saga:reconcile:{match_id}",
        f"matches:saga:reconcile:{match_id}:lease",
    )
    client.zrem(PENDING_SAGAS, match_id)
    client.zrem(SAGA_RECONCILIATION_DUE, match_id)
    client.close()


def test_reconciled_redis_event_is_published_to_real_kafka(isolated_redis_case):
    bootstrap = os.getenv("H04_KAFKA_BOOTSTRAP_SERVERS")
    if not bootstrap:
        pytest.skip("requires isolated H04-05 Kafka service")
    case = isolated_redis_case
    client = case["client"]
    repository = MatchRepository(client)
    client.delete(MATCH_EVENT_OUTBOX, f"{MATCH_EVENT_OUTBOX}:inflight")
    client.zadd(PENDING_SAGAS, {case["match_id"]: 1234})
    client.zadd(SAGA_RECONCILIATION_DUE, {case["match_id"]: 1234})
    claimed, _ = repository.claim_pending_sagas(
        now=1600, limit=1, min_age_seconds=1, max_attempts=5,
        lease_seconds=30, token_prefix="kafka-worker",
    )
    assert len(claimed) == 1
    original = claimed[0][2]
    assert repository.reemit_pending_saga(
        claimed[0][0], claimed[0][1], next_attempt_at=1630
    ) == "requeued"

    async def exercise():
        group_id = f"h04-05-test-{uuid.uuid4().hex}"
        admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
        await admin.start()
        try:
            try:
                await admin.create_topics([
                    NewTopic(MATCH_FINISHED_TOPIC, num_partitions=1, replication_factor=1)
                ])
            except TopicAlreadyExistsError:
                pass
        except Exception:
            await admin.close()
            raise
        consumer = AIOKafkaConsumer(
            MATCH_FINISHED_TOPIC,
            bootstrap_servers=bootstrap,
            group_id=group_id,
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )
        producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap,
            acks="all",
            enable_idempotence=True,
        )
        await consumer.start()
        await producer.start()
        runtime = MatchSagaRuntime(repository)
        runtime._producer = producer
        publisher = asyncio.create_task(runtime._publish_outbox())
        try:
            message = await asyncio.wait_for(consumer.getone(), timeout=15)
            assert json.loads(message.value) == original
            for _ in range(30):
                if client.llen(f"{MATCH_EVENT_OUTBOX}:inflight") == 0:
                    break
                await asyncio.sleep(0.1)
            assert client.llen(f"{MATCH_EVENT_OUTBOX}:inflight") == 0
        finally:
            runtime._stopping = True
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
            await producer.stop()
            await consumer.stop()
            await admin.close()

    asyncio.run(exercise())
