import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import redis


FINISHED_MATCH_TTL_SECONDS = 30 * 24 * 60 * 60
MATCH_EVENT_OUTBOX = "outbox:matches.finished.v1"
MATCH_EVENT_INFLIGHT = "outbox:matches.finished.v1:inflight"

_APPLY_SAGA_OUTCOME_SCRIPT = """
local match_key = KEYS[1]
local expected_match = ARGV[1]
if redis.call('TYPE', match_key).ok ~= 'string' then
    return 'retry'
end
if redis.call('GET', match_key) ~= expected_match then
    return 'retry'
end
if #KEYS > 1 then
    if redis.call('TYPE', KEYS[2]).ok ~= 'none' and redis.call('TYPE', KEYS[2]).ok ~= 'zset' then
        return 'redis_type_error'
    end
    for index = 3, #KEYS, 2 do
        local key_type = redis.call('TYPE', KEYS[index]).ok
        if key_type ~= 'none' and key_type ~= 'list' then
            return 'redis_type_error'
        end
    end
    for index = 2, #KEYS, 2 do
        local result_index = ((index - 2) / 2) + 1
        local argument_index = 4 + ((result_index - 1) * 3)
        local current_score = tonumber(redis.call('ZSCORE', KEYS[index], ARGV[argument_index]) or '0')
        local increment = tonumber(ARGV[argument_index + 1])
        if not current_score or not increment then
            return 'invalid_score'
        end
        local new_score = current_score + increment
        if new_score ~= new_score or math.abs(new_score) == math.huge then
            return 'invalid_score'
        end
    end
end

redis.call('SET', match_key, ARGV[2], 'EX', ARGV[3])
for index = 2, #KEYS, 2 do
    local result_index = ((index - 2) / 2) + 1
    local argument_index = 4 + ((result_index - 1) * 3)
    redis.call('ZINCRBY', KEYS[index], ARGV[argument_index + 1], ARGV[argument_index])
    redis.call('LPUSH', KEYS[index + 1], ARGV[argument_index + 2])
end
return 'applied'
"""


class MatchRepository:
    def __init__(self, client: redis.Redis):
        self.client = client

    @staticmethod
    def _is_valid_user_id(user_id: Any) -> bool:
        return isinstance(user_id, str) and bool(user_id.strip())

    @classmethod
    def _validated_terminal_results(
        cls,
        match_data: Dict[str, Any],
        match_id: str,
        correlation_id: str,
        results: Any,
    ) -> Optional[List[Dict[str, Any]]]:
        if (
            match_data.get("matchId") != match_id
            or match_data.get("status") != "FINISHED"
            or match_data.get("mode") not in {"CLASSIC", "TIMED", "VERSUS"}
            or type(match_data.get("finishedAt")) is not int
            or not cls._is_valid_user_id(match_data.get("caseId"))
        ):
            return None

        event = match_data.get("matchFinishedEvent")
        if (
            not isinstance(event, dict)
            or event.get("correlationId") != correlation_id
            or event.get("eventId") != match_data.get("sagaEventId")
        ):
            return None
        payload = event.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("matchId") != match_id
            or payload.get("mode") != match_data.get("mode")
            or payload.get("caseId") != match_data.get("caseId")
        ):
            return None

        mode = match_data["mode"]
        if mode == "VERSUS":
            participants = [match_data.get("player1"), match_data.get("player2")]
            if (
                not all(cls._is_valid_user_id(user_id) for user_id in participants)
                or participants[0] == participants[1]
            ):
                return None
        else:
            participants = [match_data.get("player1")]
            if not cls._is_valid_user_id(participants[0]):
                return None

        terminal_results = payload.get("results")
        if not isinstance(terminal_results, list) or len(terminal_results) != len(participants):
            return None

        expected_by_user: Dict[str, Dict[str, Any]] = {}
        for terminal_result in terminal_results:
            if not isinstance(terminal_result, dict):
                return None
            user_id = terminal_result.get("userId")
            if (
                not cls._is_valid_user_id(user_id)
                or user_id in expected_by_user
                or user_id not in participants
                or type(terminal_result.get("won")) is not bool
            ):
                return None
            terminal_xp = terminal_result.get("xpDelta")
            if terminal_xp is not None and type(terminal_xp) is not int:
                return None
            expected_by_user[user_id] = terminal_result

        if set(expected_by_user) != set(participants):
            return None
        if mode == "VERSUS":
            winners = [user_id for user_id, item in expected_by_user.items() if item["won"]]
            if len(winners) != 1 or match_data.get("winnerId") != winners[0]:
                return None
        elif mode == "CLASSIC" and not expected_by_user[participants[0]]["won"]:
            return None

        if not isinstance(results, list) or len(results) != len(participants):
            return None
        received_by_user: Dict[str, Dict[str, Any]] = {}
        for result in results:
            if not isinstance(result, dict):
                return None
            user_id = result.get("userId")
            xp_delta = result.get("xpDelta")
            if (
                not cls._is_valid_user_id(user_id)
                or user_id in received_by_user
                or user_id not in expected_by_user
                or type(result.get("won")) is not bool
                or result["won"] != expected_by_user[user_id]["won"]
                or type(xp_delta) is not int
                or (
                    "difficulty" in result
                    and result["difficulty"] != expected_by_user[user_id].get("difficulty")
                )
            ):
                return None
            if "firstWinRewarded" in result and type(result["firstWinRewarded"]) is not bool:
                return None
            try:
                if not math.isfinite(float(xp_delta)):
                    return None
            except (OverflowError, ValueError):
                return None
            received_by_user[user_id] = result

        if set(received_by_user) != set(participants):
            return None
        return [received_by_user[user_id] for user_id in participants]

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
                    event = final_match.get("matchFinishedEvent")
                    if event is not None:
                        pipeline.lpush(MATCH_EVENT_OUTBOX, json.dumps(event))
                    else:
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
                event = match_data.get("matchFinishedEvent")
                if event is not None:
                    pipeline.lpush(MATCH_EVENT_OUTBOX, json.dumps(event))
                pipeline.expire(
                    f"match:{match_id}:progress",
                    FINISHED_MATCH_TTL_SECONDS,
                )
                pipeline.execute()
                return True
            except redis.WatchError:
                return False

    def get_saga_status(self, match_id: str) -> Optional[Dict[str, Any]]:
        match = self.get_match(match_id)
        if match is None or match.get("sagaStatus") is None:
            return None
        return {
            "matchId": match_id,
            "status": match.get("sagaStatus"),
            "updatedAt": match.get("sagaUpdatedAt"),
            "correlationId": match.get("correlationId"),
            "results": match.get("sagaResults", []),
            "caseId": match.get("caseId"),
            "winnerId": match.get("winnerId"),
            "winnerName": (
                match.get("player1Name")
                if match.get("winnerId") == match.get("player1")
                else match.get("player2Name")
            ),
            "player1": match.get("player1"),
            "player2": match.get("player2"),
            "player1Name": match.get("player1Name"),
            "player2Name": match.get("player2Name"),
            "timeSeconds": match.get("timeSeconds"),
            "playerTimes": match.get("playerTimes", []),
            "solution": match.get("solution"),
        }

    def recover_inflight_match_events(self) -> int:
        recovered = 0
        while self.client.llen(MATCH_EVENT_INFLIGHT):
            event = self.client.rpoplpush(MATCH_EVENT_INFLIGHT, MATCH_EVENT_OUTBOX)
            if event is None:
                break
            recovered += 1
        return recovered

    def claim_match_event(self) -> Optional[str]:
        return self.client.rpoplpush(MATCH_EVENT_OUTBOX, MATCH_EVENT_INFLIGHT)

    def acknowledge_match_event(self, raw_event: str) -> None:
        self.client.lrem(MATCH_EVENT_INFLIGHT, 1, raw_event)

    def requeue_match_event(self, raw_event: str) -> None:
        with self.client.pipeline() as pipeline:
            pipeline.lrem(MATCH_EVENT_INFLIGHT, 1, raw_event)
            pipeline.lpush(MATCH_EVENT_OUTBOX, raw_event)
            pipeline.execute()

    def apply_saga_outcome(
        self,
        match_id: str,
        correlation_id: str,
        status: str,
        results: List[Dict[str, Any]],
    ) -> str:
        key = f"match:{match_id}"
        while True:
            raw_match = self.client.get(key)
            if raw_match is None:
                return "not_found"
            match_data = json.loads(raw_match)
            if match_data.get("matchId") != match_id:
                return "invalid"
            if match_data.get("correlationId") != correlation_id:
                return "stale"
            if status not in {"CONFIRMED", "CANCELLED"}:
                return "invalid"

            current_status = match_data.get("sagaStatus")
            if current_status != "PENDING":
                if current_status != status:
                    return "conflict"
                if status == "CONFIRMED":
                    canonical_results = self._validated_terminal_results(
                        match_data, match_id, correlation_id, results
                    )
                    if canonical_results is None or match_data.get("sagaResults") != canonical_results:
                        return "conflict"
                return "duplicate"

            if status == "CONFIRMED":
                canonical_results = self._validated_terminal_results(
                    match_data, match_id, correlation_id, results
                )
                if canonical_results is None:
                    return "invalid"
            else:
                canonical_results = []

            now = int(datetime.now(timezone.utc).timestamp())
            match_data["sagaStatus"] = status
            match_data["sagaUpdatedAt"] = now
            match_data["sagaResults"] = canonical_results
            updated_match = json.dumps(match_data)
            keys = [key]
            arguments = [raw_match, updated_match, str(FINISHED_MATCH_TTL_SECONDS)]
            if status == "CONFIRMED":
                for result in canonical_results:
                    user_id = result["userId"]
                    history_entry = {
                        "matchId": match_id,
                        "mode": match_data["mode"],
                        "caseId": match_data.get("caseId", ""),
                        "result": "WON" if result["won"] else "LOST",
                        "timestamp": match_data.get("finishedAt", now),
                    }
                    keys.extend(["ranking:global", f"user:{user_id}:history"])
                    arguments.extend([
                        user_id,
                        str(result["xpDelta"]),
                        json.dumps(history_entry),
                    ])

            outcome = self.client.eval(
                _APPLY_SAGA_OUTCOME_SCRIPT,
                len(keys),
                *keys,
                *arguments,
            )
            if outcome == "retry":
                continue
            if outcome == "applied":
                return status.lower()
            if outcome in {"redis_type_error", "invalid_score"}:
                return "invalid"
            return "invalid"

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
