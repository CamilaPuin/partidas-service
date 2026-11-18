import asyncio
import json
import logging
import os

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from aiokafka.structs import ConsumerRecord, TopicPartition

from app.observability.http_metrics import MATCH_FINISHED_PUBLISHED, MATCH_SAGA_TRANSITIONS


MATCH_FINISHED_TOPIC = "matches.finished.v1"
MATCH_OUTCOME_TOPIC = "matches.saga.confirmed.v1"
logger = logging.getLogger("uvicorn.error")


class MatchSagaRuntime:
    """Publishes the Redis outbox and applies idempotent saga outcomes."""

    def __init__(self, repository) -> None:
        self._repository = repository
        self._bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
        self._group_id = os.getenv(
            "KAFKA_GROUP_ID_PARTIDAS_SAGA", "partidas-match-saga"
        )
        self._producer: AIOKafkaProducer | None = None
        self._consumer: AIOKafkaConsumer | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        recovered = await asyncio.to_thread(self._repository.recover_inflight_match_events)
        if recovered:
            logger.warning("Recovered pending match events count=%d", recovered)
        self._task = asyncio.create_task(self._run_with_retries(), name="match-saga")
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
            except Exception as exc:
                logger.error("Match saga Kafka unavailable; retrying in %ss: %s", wait_seconds, exc)
                await self._stop_clients()
                await asyncio.sleep(wait_seconds)
                wait_seconds = min(wait_seconds * 2, 30)
            finally:
                if self._stopping:
                    await self._stop_clients()

    async def _run_once(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            client_id="partidas-match-saga-producer",
            acks="all",
            enable_idempotence=True,
        )
        self._consumer = AIOKafkaConsumer(
            MATCH_OUTCOME_TOPIC,
            bootstrap_servers=self._bootstrap_servers,
            client_id="partidas-match-saga-consumer",
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await self._producer.start()
        await self._consumer.start()
        logger.info("Match saga ready topics=%s,%s", MATCH_FINISHED_TOPIC, MATCH_OUTCOME_TOPIC)
        publisher = asyncio.create_task(self._publish_outbox())
        try:
            while not self._stopping:
                if publisher.done():
                    await publisher
                try:
                    message = await asyncio.wait_for(
                        self._consumer.getone(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                await self._process_outcome(message)
                if self._stopping:
                    return
        finally:
            publisher.cancel()
            try:
                await publisher
            except asyncio.CancelledError:
                pass

    async def _publish_outbox(self) -> None:
        while not self._stopping:
            raw_event = await asyncio.to_thread(self._repository.claim_match_event)
            if raw_event is None:
                await asyncio.sleep(0.25)
                continue
            try:
                event = json.loads(raw_event)
                match_id = event["payload"]["matchId"]
                mode = event["payload"]["mode"]
                await self._producer.send_and_wait(
                    MATCH_FINISHED_TOPIC,
                    key=match_id.encode("utf-8"),
                    value=raw_event.encode("utf-8"),
                )
                await asyncio.to_thread(self._repository.acknowledge_match_event, raw_event)
                MATCH_FINISHED_PUBLISHED.labels(mode=mode).inc()
                logger.info(
                    "MatchFinished published eventId=%s correlationId=%s matchId=%s mode=%s",
                    event.get("eventId"), event.get("correlationId"), match_id, mode,
                )
            except asyncio.CancelledError:
                await asyncio.to_thread(self._repository.requeue_match_event, raw_event)
                raise
            except Exception:
                await asyncio.to_thread(self._repository.requeue_match_event, raw_event)
                raise

    async def _process_outcome(self, message: ConsumerRecord) -> None:
        if message.value is None:
            raise ValueError("Evento de resultado de saga vacío")
        outcome = json.loads(message.value)
        payload = outcome["payload"]
        status = payload["status"]
        if status not in {"CONFIRMED", "CANCELLED"}:
            raise ValueError("Estado de saga inválido")
        result = await asyncio.to_thread(
            self._repository.apply_saga_outcome,
            payload["matchId"],
            outcome["correlationId"],
            status,
            payload.get("results", []),
        )
        if result in {"confirmed", "cancelled"}:
            match = await asyncio.to_thread(self._repository.get_match, payload["matchId"])
            mode = (match or {}).get("mode", "UNKNOWN")
            MATCH_SAGA_TRANSITIONS.labels(mode=mode, status=result.upper()).inc()
            logger.info(
                "Match saga transition matchId=%s status=%s correlationId=%s",
                payload["matchId"], result.upper(), outcome["correlationId"],
            )
        else:
            logger.warning(
                "Match saga outcome ignored matchId=%s reason=%s correlationId=%s",
                payload.get("matchId"), result, outcome.get("correlationId"),
            )
        await self._consumer.commit({
            TopicPartition(message.topic, message.partition): message.offset + 1
        })

    async def _stop_clients(self) -> None:
        consumer, self._consumer = self._consumer, None
        producer, self._producer = self._producer, None
        try:
            if consumer is not None:
                await consumer.stop()
        except (KafkaError, OSError) as exc:
            logger.warning("Error stopping match saga consumer: %s", exc)
        try:
            if producer is not None:
                await producer.stop()
        except (KafkaError, OSError) as exc:
            logger.warning("Error stopping match saga producer: %s", exc)

    @staticmethod
    def _report_task_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Match saga background task stopped unexpectedly: %s", error)
