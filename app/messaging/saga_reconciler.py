"""Bounded recovery of pending match sagas from their durable Redis event."""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from app.observability.http_metrics import (
    MATCH_SAGA_RECONCILIATION,
    MATCH_SAGA_RECONCILIATION_OLDEST_AGE,
    MATCH_SAGA_RECONCILIATION_PENDING,
)
from app.repositories.matches import SagaReconciliationResponseError

logger = logging.getLogger(__name__)


class SagaReconciler:
    def __init__(self, repository) -> None:
        self._repository = repository
        self._enabled = self._bool("MATCH_SAGA_RECONCILIATION_ENABLED", True)
        self._interval = self._positive_int("MATCH_SAGA_RECONCILIATION_INTERVAL_SECONDS", 60)
        self._min_age = self._positive_int("MATCH_SAGA_RECONCILIATION_MIN_AGE_SECONDS", 300)
        self._batch = self._positive_int("MATCH_SAGA_RECONCILIATION_BATCH_SIZE", 50)
        self._lease = self._positive_int("MATCH_SAGA_RECONCILIATION_LEASE_SECONDS", 30)
        self._max_attempts = self._positive_int("MATCH_SAGA_RECONCILIATION_MAX_ATTEMPTS", 5)
        self._backoff = self._positive_int("MATCH_SAGA_RECONCILIATION_BACKOFF_SECONDS", 30)
        self._backoff_max = self._positive_int("MATCH_SAGA_RECONCILIATION_BACKOFF_MAX_SECONDS", 300)
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._index_ready = False

    @staticmethod
    def _bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        logger.warning("Invalid %s; using default=%s", name, default)
        return default

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
            return value if value > 0 else default
        except ValueError:
            logger.warning("Invalid %s; using default=%d", name, default)
            return default

    async def start(self) -> None:
        self._stopping = False
        if not self._enabled:
            logger.info("Match saga reconciliation disabled by configuration")
            return
        self._task = asyncio.create_task(self._run(), name="match-saga-reconciler")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                if not self._index_ready:
                    await asyncio.to_thread(self._repository.rebuild_pending_saga_index)
                    self._index_ready = True
                await asyncio.to_thread(self._reconcile_once)
            except asyncio.CancelledError:
                raise
            except SagaReconciliationResponseError:
                MATCH_SAGA_RECONCILIATION.labels(result="invalid_redis_response").inc()
                logger.exception(
                    "Redis returned a malformed saga claim response; "
                    "no Python-side reconciliation action was applied"
                )
            except Exception:
                MATCH_SAGA_RECONCILIATION.labels(result="error").inc()
                logger.exception("Pending match-saga reconciliation failed")
            await asyncio.sleep(self._interval)

    def _reconcile_once(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        claims, scan_outcomes = self._repository.claim_pending_sagas(
            now=now,
            limit=self._batch,
            min_age_seconds=self._min_age,
            max_attempts=self._max_attempts,
            lease_seconds=self._lease,
            token_prefix=uuid.uuid4().hex,
        )
        for _match_id, outcome in scan_outcomes:
            MATCH_SAGA_RECONCILIATION.labels(result=outcome).inc()
        for match_id, token, _event in claims:
            attempts = self._repository.client.hget(
                f"matches:saga:reconcile:{match_id}", "attempts"
            ) or "1"
            try:
                exponent = min(max(0, int(attempts) - 1), 16)
            except (TypeError, ValueError):
                exponent = 0
            delay = min(self._backoff * (2 ** exponent), self._backoff_max)
            result = self._repository.reemit_pending_saga(
                match_id, token, next_attempt_at=now + delay
            )
            MATCH_SAGA_RECONCILIATION.labels(result=result).inc()
            if result == "requeued":
                logger.warning("Re-emitted persisted match-finished event matchId=%s", match_id)
        try:
            MATCH_SAGA_RECONCILIATION_PENDING.set(
                self._repository.client.zcard("matches:saga:pending")
            )
            oldest = self._repository.oldest_pending_saga_finished_at()
            now = int(datetime.now(timezone.utc).timestamp())
            MATCH_SAGA_RECONCILIATION_OLDEST_AGE.set(
                max(0, now - oldest) if oldest is not None else 0
            )
        except Exception:
            logger.debug("Could not update pending saga gauge", exc_info=True)
