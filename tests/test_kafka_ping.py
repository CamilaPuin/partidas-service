import asyncio
from types import SimpleNamespace

from app.messaging.kafka_ping import DLQ_TOPIC, KafkaPingRuntime


class FakeProducer:
    def __init__(self, events):
        self.events = events

    async def send_and_wait(self, topic, **kwargs):
        self.events.append(("send", topic, kwargs))


class FakeConsumer:
    def __init__(self, events):
        self.events = events

    async def commit(self, offsets):
        self.events.append(("commit", offsets))


def kafka_record(value, offset=4):
    return SimpleNamespace(
        topic="poc.ping",
        partition=0,
        offset=offset,
        key=b"key-1",
        value=value,
    )


def test_ping_event_is_deduplicated_and_offset_committed_after_processing():
    runtime = KafkaPingRuntime()
    events = []
    runtime._producer = FakeProducer(events)
    runtime._consumer = FakeConsumer(events)
    value = (
        b'{"eventId":"4c8f73e2-f4b4-4c64-8d55-19300f0a0001",'
        b'"correlationId":"c-1","occurredAt":"2026-11-11T13:00:00Z",'
        b'"payload":{"message":"ping"}}'
    )

    asyncio.run(runtime._process_message(kafka_record(value)))
    asyncio.run(runtime._process_message(kafka_record(value, offset=5)))

    assert len(runtime._processed_event_ids) == 1
    assert [event[0] for event in events] == ["commit", "commit"]
    committed_partition, committed_offset = next(iter(events[0][1].items()))
    assert committed_partition.topic == "poc.ping"
    assert committed_partition.partition == 0
    assert committed_offset == 5


def test_invalid_ping_is_sent_to_dlq_before_offset_commit():
    runtime = KafkaPingRuntime()
    events = []
    runtime._producer = FakeProducer(events)
    runtime._consumer = FakeConsumer(events)

    asyncio.run(runtime._process_message(kafka_record(b"{")))

    assert [event[0] for event in events] == ["send", "commit"]
    assert events[0][1] == DLQ_TOPIC
    assert events[0][2]["headers"][0][0] == "reason"
