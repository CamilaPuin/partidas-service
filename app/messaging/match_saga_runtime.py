import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone

import redis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.abc import ConsumerRebalanceListener
from aiokafka.errors import KafkaError
from aiokafka.structs import ConsumerRecord, TopicPartition

from app.observability.http_metrics import MATCH_FINISHED_PUBLISHED, MATCH_SAGA_TRANSITIONS


MATCH_FINISHED_TOPIC = "matches.finished.v1"
MATCH_OUTCOME_TOPIC = "matches.saga.confirmed.v1"
MATCH_OUTCOME_DLQ_TOPIC = f"{MATCH_OUTCOME_TOPIC}.dlq"
logger = logging.getLogger("uvicorn.error")


class InvalidOutcome(ValueError):
    """A permanently malformed event that can be preserved in the DLQ."""


class TransientOutcomeFailure(Exception):
    """A message operation failed transiently and must retain its offset."""


class OffsetCommitFailure(TransientOutcomeFailure):
    """Processing is complete; retry only the Kafka offset commit."""


class _OutcomeRebalanceListener(ConsumerRebalanceListener):
    def __init__(self, runtime):
        self._runtime = runtime

    async def on_partitions_revoked(self, revoked) -> None:
        await self._runtime._cancel_revoked_partitions(revoked)

    async def on_partitions_assigned(self, assigned) -> None:
        logger.info("Saga outcome partitions assigned count=%d", len(assigned))


class MatchSagaRuntime:
    """Publishes the Redis outbox and applies idempotent saga outcomes."""

    def __init__(self, repository) -> None:
        self._repository = repository
        self._bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
        self._group_id = os.getenv(
            "KAFKA_GROUP_ID_PARTIDAS_SAGA", "partidas-match-saga"
        )
        self._retry_max_attempts = self._positive_int(
            "KAFKA_SAGA_RETRY_MAX_ATTEMPTS", 3
        )
        self._retry_backoff_seconds = self._non_negative_float(
            "KAFKA_SAGA_RETRY_BACKOFF_SECONDS", 0.5
        )
        self._retry_backoff_max_seconds = self._non_negative_float(
            "KAFKA_SAGA_RETRY_BACKOFF_MAX_SECONDS", 10.0
        )
        self._retry_backoff_max_seconds = max(0.1, self._retry_backoff_max_seconds)
        self._retry_backoff_seconds = min(
            self._retry_backoff_seconds, self._retry_backoff_max_seconds
        )
        self._producer: AIOKafkaProducer | None = None
        self._consumer: AIOKafkaConsumer | None = None
        self._task: asyncio.Task[None] | None = None
        self._partition_workers: dict[TopicPartition, asyncio.Task[None]] = {}
        self._partition_queues: dict[TopicPartition, asyncio.Queue] = {}
        self._rebalance_listener = _OutcomeRebalanceListener(self)
        self._stopping = False

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, str(default))))
        except ValueError:
            logger.warning("Invalid %s; using default=%s", name, default)
            return default

    @staticmethod
    def _non_negative_float(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
            if not math.isfinite(value) or value < 0:
                raise ValueError
            return value
        except ValueError:
            logger.warning("Invalid %s; using default=%s", name, default)
            return default

    async def start(self) -> None:
        self._stopping = False
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
                recovered = await asyncio.to_thread(
                    self._repository.recover_inflight_match_events
                )
                if recovered:
                    logger.warning("Recovered pending match events count=%d", recovered)
                await self._run_once()
                wait_seconds = 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Match saga Kafka runtime failed; reconnecting in %ss",
                    wait_seconds,
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
            client_id="partidas-match-saga-producer",
            acks="all",
            enable_idempotence=True,
        )
        self._consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap_servers,
            client_id="partidas-match-saga-consumer",
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        self._consumer.subscribe(
            topics=[MATCH_OUTCOME_TOPIC], listener=self._rebalance_listener
        )
        await self._producer.start()
        await self._consumer.start()
        logger.info(
            "Match saga ready topics=%s,%s dlq=%s group=%s",
            MATCH_FINISHED_TOPIC,
            MATCH_OUTCOME_TOPIC,
            MATCH_OUTCOME_DLQ_TOPIC,
            self._group_id,
        )
        publisher = asyncio.create_task(self._publish_outbox())
        try:
            while not self._stopping:
                if publisher.done():
                    await publisher
                try:
                    message = await asyncio.wait_for(self._consumer.getone(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                partition = TopicPartition(message.topic, message.partition)
                queue = self._partition_queues.setdefault(partition, asyncio.Queue())
                if partition not in self._partition_workers:
                    self._consumer.pause(partition)
                    worker = asyncio.create_task(
                        self._process_partition(partition, queue),
                        name=f"match-saga-{message.topic}-{message.partition}",
                    )
                    self._partition_workers[partition] = worker
                queue.put_nowait(message)
        finally:
            publisher.cancel()
            workers = list(self._partition_workers.values())
            for worker in workers:
                worker.cancel()
            await asyncio.gather(publisher, *workers, return_exceptions=True)
            self._partition_workers.clear()
            self._partition_queues.clear()

    async def _process_partition(
        self, partition: TopicPartition, queue: asyncio.Queue
    ) -> None:
        try:
            while not self._stopping:
                try:
                    message = queue.get_nowait()
                except asyncio.QueueEmpty:
                    self._partition_workers.pop(partition, None)
                    self._partition_queues.pop(partition, None)
                    if self._consumer is not None:
                        self._consumer.resume(partition)
                    return

                retry_delay = max(0.1, self._retry_backoff_seconds)
                while not self._stopping:
                    try:
                        await self._process_outcome_with_retries(message)
                        break
                    except asyncio.CancelledError:
                        raise
                    except TransientOutcomeFailure as exc:
                        logger.error(
                            "Saga outcome remains uncommitted; partition paused "
                            "topic=%s partition=%d offset=%d attempts=%d error_type=%s",
                            message.topic,
                            message.partition,
                            message.offset,
                            self._retry_max_attempts,
                            type(exc.__cause__ or exc).__name__,
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay = self._next_backoff(retry_delay)
                    except Exception:
                        # Programming errors must not be converted to invalid events or
                        # acknowledged. Keep this partition ordered while others run.
                        logger.exception(
                            "Unexpected saga outcome error; retaining offset "
                            "topic=%s partition=%d offset=%d",
                            message.topic,
                            message.partition,
                            message.offset,
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay = self._next_backoff(retry_delay)
                queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            if self._partition_workers.get(partition) is asyncio.current_task():
                self._partition_workers.pop(partition, None)
            if self._stopping:
                self._partition_queues.pop(partition, None)

    def _next_backoff(self, current: float) -> float:
        return min(current * 2, max(0.1, self._retry_backoff_max_seconds))

    async def _cancel_revoked_partitions(self, revoked) -> None:
        workers = []
        for partition in revoked:
            worker = self._partition_workers.pop(partition, None)
            if worker is not None:
                worker.cancel()
                workers.append(worker)
            self._partition_queues.pop(partition, None)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _process_outcome_with_retries(self, message: ConsumerRecord) -> None:
        delay = self._retry_backoff_seconds
        for attempt in range(1, self._retry_max_attempts + 1):
            try:
                await self._apply_outcome(message)
                break
            except asyncio.CancelledError:
                raise
            except TransientOutcomeFailure as exc:
                logger.warning(
                    "Transient saga outcome failure attempt=%d/%d topic=%s "
                    "partition=%d offset=%d error_type=%s",
                    attempt,
                    self._retry_max_attempts,
                    message.topic,
                    message.partition,
                    message.offset,
                    type(exc.__cause__ or exc).__name__,
                )
                if attempt == self._retry_max_attempts:
                    raise
                await asyncio.sleep(delay)
                delay = self._next_backoff(delay)

        # The Redis/DLQ stage has completed. Retrying only the commit avoids
        # repeating application work in this process; after a process restart,
        # Kafka may redeliver and the Redis Lua transition remains authoritative.
        commit_delay = max(0.1, self._retry_backoff_seconds)
        while not self._stopping:
            try:
                await self._commit_message(message)
                return
            except OffsetCommitFailure as exc:
                logger.warning(
                    "Offset commit retry topic=%s partition=%d offset=%d "
                    "next_offset=%d group=%s error_type=%s",
                    message.topic,
                    message.partition,
                    message.offset,
                    message.offset + 1,
                    self._group_id,
                    type(exc.__cause__ or exc).__name__,
                )
                await asyncio.sleep(commit_delay)
                commit_delay = self._next_backoff(commit_delay)

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

    @staticmethod
    def _decode_outcome(raw_value: bytes | None) -> dict:
        if raw_value is None:
            raise InvalidOutcome("message value is null")
        try:
            outcome = json.loads(raw_value)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidOutcome("message is not valid UTF-8 JSON") from exc
        if not isinstance(outcome, dict):
            raise InvalidOutcome("event must be a JSON object")
        correlation_id = outcome.get("correlationId")
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise InvalidOutcome("correlationId must be a non-empty string")
        payload = outcome.get("payload")
        if not isinstance(payload, dict):
            raise InvalidOutcome("payload must be a JSON object")
        match_id = payload.get("matchId")
        if not isinstance(match_id, str) or not match_id.strip():
            raise InvalidOutcome("payload.matchId must be a non-empty string")
        status = payload.get("status")
        if not isinstance(status, str) or status not in {"CONFIRMED", "CANCELLED"}:
            raise InvalidOutcome("payload.status must be CONFIRMED or CANCELLED")
        results = payload.get("results", [])
        if status == "CONFIRMED" and "results" not in payload:
            raise InvalidOutcome("payload.results is required for CONFIRMED")
        if not isinstance(results, list):
            raise InvalidOutcome("payload.results must be an array")
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                raise InvalidOutcome(f"payload.results[{index}] must be an object")
            if not isinstance(result.get("userId"), str) or not result["userId"].strip():
                raise InvalidOutcome(f"payload.results[{index}].userId must be a non-empty string")
            if not isinstance(result.get("won"), bool):
                raise InvalidOutcome(f"payload.results[{index}].won must be boolean")
            xp_delta = result.get("xpDelta")
            if isinstance(xp_delta, bool) or not isinstance(xp_delta, (int, float)):
                raise InvalidOutcome(f"payload.results[{index}].xpDelta must be numeric")
            try:
                finite_xp = math.isfinite(xp_delta)
            except OverflowError:
                finite_xp = False
            if not finite_xp:
                raise InvalidOutcome(f"payload.results[{index}].xpDelta must be finite")
        outcome["payload"]["results"] = results
        return outcome

    async def _process_outcome(self, message: ConsumerRecord) -> None:
        await self._apply_outcome(message)
        await self._commit_message(message)

    async def _apply_outcome(self, message: ConsumerRecord) -> None:
        try:
            outcome = self._decode_outcome(message.value)
        except InvalidOutcome as exc:
            await self._send_to_dlq(message, exc, outcome=None)
            return

        payload = outcome["payload"]
        try:
            result = await asyncio.to_thread(
                self._repository.apply_saga_outcome,
                payload["matchId"],
                outcome["correlationId"],
                payload["status"],
                payload["results"],
            )
        except (redis.RedisError, OSError, asyncio.TimeoutError) as exc:
            raise TransientOutcomeFailure("Redis operation failed") from exc

        if result in {"confirmed", "cancelled"}:
            try:
                match = await asyncio.to_thread(self._repository.get_match, payload["matchId"])
            except (redis.RedisError, OSError, asyncio.TimeoutError) as exc:
                raise TransientOutcomeFailure("Redis read failed after applying saga outcome") from exc
            mode = (match or {}).get("mode", "UNKNOWN")
            MATCH_SAGA_TRANSITIONS.labels(mode=mode, status=result.upper()).inc()
            logger.info(
                "Match saga transition matchId=%s status=%s correlationId=%s",
                payload["matchId"], result.upper(), outcome["correlationId"],
            )
        elif result == "duplicate":
            logger.info(
                "Duplicate match saga outcome acknowledged matchId=%s correlationId=%s",
                payload["matchId"], outcome["correlationId"],
            )
        elif result == "stale":
            # Correlation mismatch is a legitimate late event from an older saga.
            # Redis has verified it cannot mutate the current match, so it is safe to skip.
            logger.warning(
                "Stale match saga outcome discarded matchId=%s correlationId=%s "
                "partition=%d offset=%d",
                payload["matchId"], outcome["correlationId"],
                message.partition, message.offset,
            )
        elif result in {"invalid", "conflict", "not_found"}:
            descriptions = {
                "invalid": "outcome does not match persisted terminal data",
                "conflict": "outcome contradicts a previously applied transition",
                "not_found": "match is absent from the saga repository",
            }
            await self._send_to_dlq(
                message,
                ValueError(descriptions[result]),
                outcome=outcome,
                error_type=f"business_{result}",
            )
        else:
            raise RuntimeError(f"Unknown apply_saga_outcome result: {result!r}")

    async def _send_to_dlq(
        self,
        message: ConsumerRecord,
        error: Exception,
        outcome: dict | None,
        error_type: str | None = None,
    ) -> None:
        correlation_id = (outcome or {}).get("correlationId")
        headers = [
            ("original-topic", message.topic.encode("utf-8")),
            ("original-partition", str(message.partition).encode("ascii")),
            ("original-offset", str(message.offset).encode("ascii")),
            ("error-type", (error_type or type(error).__name__).encode("utf-8")[:128]),
            ("error-description", str(error).encode("utf-8", "replace")[:1024]),
            ("failed-at", datetime.now(timezone.utc).isoformat().encode("ascii")),
            ("attempts", b"1"),
        ]
        if isinstance(correlation_id, str) and correlation_id:
            headers.append(("correlation-id", correlation_id.encode("utf-8")[:256]))
        # Keep the payload and Kafka key byte-for-byte intact. send_and_wait resolves
        # only after the broker acknowledges the DLQ record.
        try:
            await self._producer.send_and_wait(
                MATCH_OUTCOME_DLQ_TOPIC,
                key=message.key,
                value=message.value,
                headers=headers,
            )
        except (KafkaError, OSError, asyncio.TimeoutError) as exc:
            raise TransientOutcomeFailure("DLQ publish failed") from exc
        logger.warning(
            "Saga outcome routed to DLQ topic=%s original_topic=%s "
            "partition=%d offset=%d error_type=%s",
            MATCH_OUTCOME_DLQ_TOPIC,
            message.topic,
            message.partition,
            message.offset,
            error_type or type(error).__name__,
        )

    async def _commit_message(self, message: ConsumerRecord) -> None:
        partition = TopicPartition(message.topic, message.partition)
        try:
            await self._consumer.commit({partition: message.offset + 1})
        except (KafkaError, OSError, asyncio.TimeoutError) as exc:
            consumer = self._consumer
            assigned = None
            if consumer is not None:
                assignment = getattr(consumer, "assignment", None)
                if callable(assignment):
                    assigned = partition in assignment()
            logger.warning(
                "Kafka offset commit failed topic=%s partition=%d offset=%d "
                "next_offset=%d group=%s assigned=%s error_type=%s",
                message.topic,
                message.partition,
                message.offset,
                message.offset + 1,
                self._group_id,
                assigned,
                type(exc).__name__,
            )
            raise OffsetCommitFailure("Kafka offset commit failed") from exc
        logger.debug(
            "Kafka offset commit acknowledged topic=%s partition=%d "
            "next_offset=%d group=%s",
            message.topic,
            message.partition,
            message.offset + 1,
            self._group_id,
        )

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
