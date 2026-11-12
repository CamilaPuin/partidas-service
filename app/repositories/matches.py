import json
from typing import Any, Dict, List, Optional, Tuple

import redis


FINISHED_MATCH_TTL_SECONDS = 30 * 24 * 60 * 60


class MatchRepository:
    def __init__(self, client: redis.Redis):
        self.client = client

    @staticmethod
    def _classic_attempt_key(match_id: str, attempt_id: str) -> str:
        return f"match:{match_id}:classic-attempt:{attempt_id}"

    @staticmethod
    def _classic_attempts_key(match_id: str) -> str:
        return f"match:{match_id}:classic-attempts"

    def get_match(self, match_id: str) -> Optional[Dict[str, Any]]:
        raw_match = self.client.get(f"match:{match_id}")
        return json.loads(raw_match) if raw_match else None

    def save_match(self, match_id: str, match_data: Dict[str, Any]) -> None:
        self.client.set(f"match:{match_id}", json.dumps(match_data))

    def reserve_classic_attempt(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
    ) -> Dict[str, Any]:
        match_key = f"match:{match_id}"
        attempt_key = self._classic_attempt_key(match_id, attempt_id)
        while True:
            with self.client.pipeline() as pipeline:
                try:
                    pipeline.watch(match_key, attempt_key)
                    raw_match = pipeline.get(match_key)
                    if not raw_match:
                        return {"action": "not_found"}

                    match_data = json.loads(raw_match)
                    raw_attempt = pipeline.get(attempt_key)
                    if raw_attempt:
                        attempt = json.loads(raw_attempt)
                        if attempt.get("fingerprint") != fingerprint:
                            return {"action": "conflict"}
                        return {"action": "replay", "result": attempt["result"]}

                    status = match_data.get("status")
                    if status not in {"IN_PROGRESS", "VALIDATING", "FINALIZING"}:
                        return {"action": "unavailable"}

                    active_attempt = match_data.get("activeAttempt")
                    if active_attempt:
                        if (
                            active_attempt.get("attemptId") != attempt_id
                            or active_attempt.get("fingerprint") != fingerprint
                        ):
                            return {"action": "conflict"}
                        return {
                            "action": (
                                "finalize"
                                if status == "FINALIZING"
                                else "validate"
                            ),
                            "match": match_data,
                        }

                    if status != "IN_PROGRESS":
                        return {"action": "unavailable"}

                    match_data["status"] = "VALIDATING"
                    match_data["activeAttempt"] = {
                        "attemptId": attempt_id,
                        "fingerprint": fingerprint,
                    }
                    pipeline.multi()
                    pipeline.set(match_key, json.dumps(match_data))
                    pipeline.execute()
                    return {"action": "validate", "match": match_data}
                except redis.WatchError:
                    continue

    def release_classic_validation(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
    ) -> bool:
        key = f"match:{match_id}"
        while True:
            with self.client.pipeline() as pipeline:
                try:
                    pipeline.watch(key)
                    raw_match = pipeline.get(key)
                    if not raw_match:
                        return False
                    match_data = json.loads(raw_match)
                    active_attempt = match_data.get("activeAttempt") or {}
                    if (
                        match_data.get("status") != "VALIDATING"
                        or active_attempt.get("attemptId") != attempt_id
                        or active_attempt.get("fingerprint") != fingerprint
                    ):
                        return False

                    match_data["status"] = "IN_PROGRESS"
                    match_data.pop("activeAttempt", None)
                    pipeline.multi()
                    pipeline.set(key, json.dumps(match_data))
                    pipeline.execute()
                    return True
                except redis.WatchError:
                    continue

    def mark_classic_finalizing(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
        time_seconds: int,
        finalized_at: int,
    ) -> Dict[str, Any]:
        match_key = f"match:{match_id}"
        attempt_key = self._classic_attempt_key(match_id, attempt_id)
        while True:
            with self.client.pipeline() as pipeline:
                try:
                    pipeline.watch(match_key, attempt_key)
                    raw_match = pipeline.get(match_key)
                    if not raw_match:
                        return {"action": "not_found"}
                    raw_attempt = pipeline.get(attempt_key)
                    if raw_attempt:
                        attempt = json.loads(raw_attempt)
                        if attempt.get("fingerprint") != fingerprint:
                            return {"action": "conflict"}
                        return {"action": "replay", "result": attempt["result"]}

                    match_data = json.loads(raw_match)
                    active_attempt = match_data.get("activeAttempt") or {}
                    if (
                        active_attempt.get("attemptId") != attempt_id
                        or active_attempt.get("fingerprint") != fingerprint
                    ):
                        return {"action": "conflict"}
                    if match_data.get("status") == "VALIDATING":
                        match_data["status"] = "FINALIZING"
                        match_data["finalizingTimeSeconds"] = time_seconds
                        match_data["finalizingAt"] = finalized_at
                        pipeline.multi()
                        pipeline.set(match_key, json.dumps(match_data))
                        pipeline.execute()
                    elif match_data.get("status") != "FINALIZING":
                        return {"action": "unavailable"}
                    if (
                        type(match_data.get("finalizingTimeSeconds")) is not int
                        or type(match_data.get("finalizingAt")) is not int
                    ):
                        return {"action": "unavailable"}
                    return {"action": "finalize", "match": match_data}
                except redis.WatchError:
                    continue

    def finish_classic_incorrect_attempt(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        match_key = f"match:{match_id}"
        attempt_key = self._classic_attempt_key(match_id, attempt_id)
        attempts_key = self._classic_attempts_key(match_id)
        while True:
            with self.client.pipeline() as pipeline:
                try:
                    pipeline.watch(match_key, attempt_key)
                    raw_match = pipeline.get(match_key)
                    if not raw_match:
                        return {"action": "not_found"}
                    raw_attempt = pipeline.get(attempt_key)
                    if raw_attempt:
                        attempt = json.loads(raw_attempt)
                        if attempt.get("fingerprint") != fingerprint:
                            return {"action": "conflict"}
                        return {"action": "replay", "result": attempt["result"]}

                    match_data = json.loads(raw_match)
                    active_attempt = match_data.get("activeAttempt") or {}
                    if (
                        match_data.get("status") != "VALIDATING"
                        or active_attempt.get("attemptId") != attempt_id
                        or active_attempt.get("fingerprint") != fingerprint
                    ):
                        return {"action": "unavailable"}

                    match_data["status"] = "IN_PROGRESS"
                    match_data.pop("activeAttempt", None)
                    pipeline.multi()
                    pipeline.set(match_key, json.dumps(match_data))
                    pipeline.set(
                        attempt_key,
                        json.dumps({"fingerprint": fingerprint, "result": result}),
                    )
                    pipeline.sadd(attempts_key, attempt_key)
                    pipeline.execute()
                    return {"action": "stored", "result": result}
                except redis.WatchError:
                    continue

    def complete_classic_attempt(
        self,
        match_id: str,
        attempt_id: str,
        fingerprint: str,
        match_data: Dict[str, Any],
        result: Dict[str, Any],
        user_id: str,
        score_delta: int,
        history_entry: Dict[str, Any],
    ) -> Dict[str, Any]:
        match_key = f"match:{match_id}"
        attempt_key = self._classic_attempt_key(match_id, attempt_id)
        attempts_key = self._classic_attempts_key(match_id)
        while True:
            with self.client.pipeline() as pipeline:
                try:
                    pipeline.watch(match_key, attempt_key, attempts_key)
                    raw_match = pipeline.get(match_key)
                    if not raw_match:
                        return {"action": "not_found"}
                    raw_attempt = pipeline.get(attempt_key)
                    if raw_attempt:
                        attempt = json.loads(raw_attempt)
                        if attempt.get("fingerprint") != fingerprint:
                            return {"action": "conflict"}
                        return {"action": "replay", "result": attempt["result"]}

                    current_match = json.loads(raw_match)
                    active_attempt = current_match.get("activeAttempt") or {}
                    if (
                        current_match.get("status") != "FINALIZING"
                        or active_attempt.get("attemptId") != attempt_id
                        or active_attempt.get("fingerprint") != fingerprint
                    ):
                        return {"action": "unavailable"}

                    attempt_keys = pipeline.smembers(attempts_key)
                    final_match = dict(match_data)
                    final_match["status"] = "FINISHED"
                    final_match["finalResult"] = result
                    final_match.pop("activeAttempt", None)
                    pipeline.multi()
                    pipeline.set(
                        match_key,
                        json.dumps(final_match),
                        ex=FINISHED_MATCH_TTL_SECONDS,
                    )
                    pipeline.set(
                        attempt_key,
                        json.dumps({"fingerprint": fingerprint, "result": result}),
                        ex=FINISHED_MATCH_TTL_SECONDS,
                    )
                    pipeline.sadd(attempts_key, attempt_key)
                    pipeline.zincrby("ranking:global", score_delta, user_id)
                    pipeline.lpush(
                        f"user:{user_id}:history",
                        json.dumps(history_entry),
                    )
                    pipeline.expire(
                        f"match:{match_id}:progress",
                        FINISHED_MATCH_TTL_SECONDS,
                    )
                    for stored_attempt_key in attempt_keys:
                        pipeline.expire(
                            stored_attempt_key,
                            FINISHED_MATCH_TTL_SECONDS,
                        )
                    pipeline.expire(attempts_key, FINISHED_MATCH_TTL_SECONDS)
                    pipeline.execute()
                    return {"action": "stored", "result": result}
                except redis.WatchError:
                    continue

    def complete_match(
        self,
        match_id: str,
        expected_status: str,
        match_data: Dict[str, Any],
    ) -> bool:
        key = f"match:{match_id}"
        with self.client.pipeline() as pipeline:
            try:
                pipeline.watch(key)
                raw_match = pipeline.get(key)
                if not raw_match:
                    pipeline.unwatch()
                    return False

                current_match = json.loads(raw_match)
                if current_match.get("status") != expected_status:
                    pipeline.unwatch()
                    return False

                pipeline.multi()
                pipeline.set(
                    key,
                    json.dumps(match_data),
                    ex=FINISHED_MATCH_TTL_SECONDS,
                )
                pipeline.expire(
                    f"match:{match_id}:progress",
                    FINISHED_MATCH_TTL_SECONDS,
                )
                pipeline.execute()
                return True
            except redis.WatchError:
                return False

    def add_open_match(self, match_id: str) -> None:
        self.client.sadd("matches:open", match_id)

    def remove_open_match(self, match_id: str) -> None:
        self.client.srem("matches:open", match_id)

    def list_open_match_ids(self) -> set:
        return self.client.smembers("matches:open")

    def save_progress(self, match_id: str, user_id: str, progress: float) -> None:
        self.client.hset(f"match:{match_id}:progress", user_id, progress)

    def get_progress(self, match_id: str, user_id: str) -> float:
        return float(self.client.hget(f"match:{match_id}:progress", user_id) or 0.0)

    def increment_score(self, user_id: str, delta: int) -> None:
        self.client.zincrby("ranking:global", delta, user_id)

    def list_ranking(self, limit: int) -> List[Tuple[str, float]]:
        return self.client.zrevrange(
            "ranking:global",
            0,
            limit - 1,
            withscores=True,
        )

    def save_user_history(self, user_id: str, history_entry: Dict[str, Any]) -> None:
        self.client.lpush(
            f"user:{user_id}:history",
            json.dumps(history_entry),
        )

    def list_user_history(self, user_id: str) -> List[Dict[str, Any]]:
        history_items = self.client.lrange(f"user:{user_id}:history", 0, -1)
        return [json.loads(item) for item in history_items]
