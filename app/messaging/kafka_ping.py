import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from aiokafka.structs import ConsumerRecord, TopicPartition
from pydantic import ValidationError

from app.messaging.ping_event import PingEvent

PING_TOPIC = "poc.ping"
DLQ_TOPIC = "poc.ping.dlq"
logger = logging.getLogger("uvicorn.error")


class KafkaPingRuntime:
    def __init__(self) -> None:
        self._bootstrap_servers = os.getenv(
            "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"
        )
        self._group_id = os.getenv("KAFKA_GROUP_ID", "partidas-service")
        self._publish_on_start = (
            os.getenv("KAFKA_POC_PING_ON_START", "false").strip().lower()
            == "true"
        )
        self._processed_event_ids: set[str] = set()
        self._producer: AIOKafkaProducer | None = None
        self._consumer: AIOKafkaConsumer | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(
            self._run_with_retries(), name="kafka-ping"
        )
        self._task.add_done_callback(self._report_task_result)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._stop_clients()

    async def _run_with_retries(self) -> None:
        wait_seconds = 1
        while not self._stopping:
            try:
                await self._run_once()
                wait_seconds = 1
            except asyncio.CancelledError:
                raise
            except (KafkaError, OSError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Kafka unavailable; retrying in %ss: %s",
                    wait_seconds,
                    exc,
                )
                await self._stop_clients()
                await asyncio.sleep(wait_seconds)
                wait_seconds = min(wait_seconds * 2, 30)
            finally:
                if self._stopping:
                    await self._stop_clients()

    async def _run_once(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            client_id="partidas-service",
        )
        self._consumer = AIOKafkaConsumer(
            PING_TOPIC,
            bootstrap_servers=self._bootstrap_servers,
            client_id="partidas-service",
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await self._producer.start()
        await self._consumer.start()
        logger.info("Kafka consumer ready for %s", PING_TOPIC)

        if self._publish_on_start:
            await self._publish_startup_ping()

        async for message in self._consumer:
            await self._process_message(message)
            if self._stopping:
                return

    async def _publish_startup_ping(self) -> None:
        event = PingEvent(
            eventId=str(uuid4()),
            correlationId=str(uuid4()),
            occurredAt=datetime.now(timezone.utc),
            payload={"message": "ping from partidas-service"},
        )
        await self._producer.send_and_wait(
            PING_TOPIC,
            key=event.correlation_id.encode(),
            value=json.dumps(event.model_dump(by_alias=True), default=str).encode(),
        )
        logger.info(
            "Kafka ping published eventId=%s correlationId=%s",
            event.event_id,
            event.correlation_id,
        )

    async def _process_message(self, message: ConsumerRecord) -> None:
        if message.value is None:
            await self._send_invalid_message(message, "message value is null")
            return
        try:
            event = PingEvent.model_validate_json(message.value)
        except ValidationError as exc:
            await self._send_invalid_message(message, str(exc))
            return

        event_id = str(event.event_id)
        if event_id in self._processed_event_ids:
            logger.warning("Duplicate ping ignored eventId=%s", event_id)
        else:
            self._processed_event_ids.add(event_id)
            logger.info(
                "ping recibido eventId=%s correlationId=%s",
                event_id,
                event.correlation_id,
            )
        await self._commit_message(message)

    async def _commit_message(self, message: ConsumerRecord) -> None:
        await self._consumer.commit(
            {
                TopicPartition(message.topic, message.partition):
                    message.offset + 1
            }
        )

    async def _send_invalid_message(
        self, message: ConsumerRecord, reason: str
    ) -> None:
        await self._producer.send_and_wait(
            DLQ_TOPIC,
            key=message.key,
            value=message.value,
            headers=[("reason", reason.encode("utf-8")[:1024])],
        )
        logger.warning("Invalid Kafka event sent to %s", DLQ_TOPIC)
        await self._commit_message(message)

    async def _stop_clients(self) -> None:
        consumer, self._consumer = self._consumer, None
        producer, self._producer = self._producer, None
        try:
            if consumer is not None:
                await consumer.stop()
        except (KafkaError, OSError) as exc:
            logger.warning("Error stopping Kafka consumer: %s", exc)
        try:
            if producer is not None:
                await producer.stop()
        except (KafkaError, OSError) as exc:
            logger.warning("Error stopping Kafka producer: %s", exc)

    @staticmethod
    def _report_task_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Kafka ping background task stopped unexpectedly: %s",
                error,
            )
