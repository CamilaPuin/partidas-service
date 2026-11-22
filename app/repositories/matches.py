import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import redis


FINISHED_MATCH_TTL_SECONDS = 30 * 24 * 60 * 60
MATCH_EVENT_OUTBOX = "outbox:matches.finished.v1"
MATCH_EVENT_INFLIGHT = "outbox:matches.finished.v1:inflight"
PENDING_SAGAS = "matches:saga:pending"
SAGA_RECONCILIATION_DUE = "matches:saga:reconcile:due"
SAGA_RECONCILIATION_INDEX_READY = "matches:saga:reconcile:index-ready:v1"


class SagaReconciliationResponseError(RuntimeError):
    """Redis returned a malformed response for the saga claim script."""


def _decode_saga_claim_response(values: Any) -> tuple[list[tuple[str, str, dict[str, Any]]], list[tuple[str, str]]]:
    """Decode the nested two-array response returned by _CLAIM_SAGAS_SCRIPT."""
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise SagaReconciliationResponseError(
            "Redis saga claim response must contain [claims, outcomes]"
        )
    raw_claims, raw_outcomes = values
    if not isinstance(raw_claims, (list, tuple)) or not isinstance(raw_outcomes, (list, tuple)):
        raise SagaReconciliationResponseError(
            "Redis saga claim response claims and outcomes must be arrays"
        )
    if len(raw_claims) % 3:
        raise SagaReconciliationResponseError(
            f"Redis saga claim response has {len(raw_claims)} claim fields; expected triples"
        )
    if len(raw_outcomes) % 2:
        raise SagaReconciliationResponseError(
            f"Redis saga claim response has {len(raw_outcomes)} outcome fields; expected pairs"
        )

    def text_field(value: Any, field: str) -> str:
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise SagaReconciliationResponseError(
                    f"Redis saga claim response field {field} is not valid UTF-8"
                ) from exc
        if not isinstance(value, str) or not value:
            raise SagaReconciliationResponseError(
                f"Redis saga claim response field {field} must be a non-empty string"
            )
        return value

    claims: list[tuple[str, str, dict[str, Any]]] = []
    for offset in range(0, len(raw_claims), 3):
        match_id = text_field(raw_claims[offset], "claim.match_id")
        token = text_field(raw_claims[offset + 1], "claim.lease_token")
        raw_event = text_field(raw_claims[offset + 2], "claim.event")
        try:
            event = json.loads(raw_event)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SagaReconciliationResponseError(
                f"Redis saga claim response contains invalid event JSON for {match_id}"
            ) from exc
        if not isinstance(event, dict):
            raise SagaReconciliationResponseError(
                f"Redis saga claim response event for {match_id} must be an object"
            )
        claims.append((match_id, token, event))

    valid_outcomes = {
        "stale", "unrecoverable", "already_queued", "exhausted",
        "lease_expired", "leased",
    }
    outcomes: list[tuple[str, str]] = []
    for offset in range(0, len(raw_outcomes), 2):
        match_id = text_field(raw_outcomes[offset], "outcome.match_id")
        outcome = text_field(raw_outcomes[offset + 1], "outcome.result")
        if outcome not in valid_outcomes:
            raise SagaReconciliationResponseError(
                f"Redis saga claim response has unknown outcome {outcome!r}"
            )
        outcomes.append((match_id, outcome))
    return claims, outcomes


_CLAIM_SAGAS_SCRIPT = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
local claimed = {}
local outcomes = {}
local function valid_event(match, event, match_id)
    local payload = type(event) == 'table' and event.payload or nil
    local expected_count = match.mode == 'VERSUS' and 2 or 1
    if match.matchId ~= match_id or match.status ~= 'FINISHED'
        or (match.mode ~= 'CLASSIC' and match.mode ~= 'TIMED' and match.mode ~= 'VERSUS')
        or type(match.caseId) ~= 'string' or #match.caseId == 0
        or type(match.sagaEventId) ~= 'string' or #match.sagaEventId == 0
        or type(match.correlationId) ~= 'string' or #match.correlationId == 0
        or type(event) ~= 'table' or event.eventId ~= match.sagaEventId
        or type(event.occurredAt) ~= 'string' or event.correlationId ~= match.correlationId
        or type(payload) ~= 'table'
        or payload.matchId ~= match_id or payload.caseId ~= match.caseId
        or payload.mode ~= match.mode or type(payload.results) ~= 'table'
        or type(payload.timeSeconds) ~= 'number' or payload.timeSeconds < 0
        or #payload.results ~= expected_count then return false end
    for index, result in ipairs(payload.results) do
        local participant = index == 1 and match.player1 or match.player2
        if type(participant) ~= 'string' or #participant == 0
            or type(result) ~= 'table' or result.userId ~= participant
            or type(result.won) ~= 'boolean'
            or (match.mode == 'CLASSIC' and (
                type(result.difficulty) ~= 'string' or #result.difficulty == 0
                or result.xpDelta ~= cjson.null
            ))
            or (match.mode ~= 'CLASSIC' and type(result.xpDelta) ~= 'number') then
            return false
        end
    end
    return true
end
local function event_is_queued(event_id)
    for _, list_key in ipairs({'outbox:matches.finished.v1', 'outbox:matches.finished.v1:inflight'}) do
        local recent = redis.call('LRANGE', list_key, 0, 199)
        for _, queued_raw in ipairs(recent) do
            local queued_ok, queued_event = pcall(cjson.decode, queued_raw)
            if queued_ok and type(queued_event) == 'table' and queued_event.eventId == event_id then
                return true
            end
        end
    end
    return false
end
for _, match_id in ipairs(ids) do
    local match_key = 'match:' .. match_id
    local meta_key = 'matches:saga:reconcile:' .. match_id
    local lease_key = meta_key .. ':lease'
    local raw = nil
    if redis.call('TYPE', match_key).ok == 'string' then raw = redis.call('GET', match_key) end
    local ok, match = pcall(cjson.decode, raw or '')
    if not ok or type(match) ~= 'table' or match.sagaStatus ~= 'PENDING' then
        redis.call('ZREM', KEYS[1], match_id)
        redis.call('ZREM', KEYS[2], match_id)
        redis.call('DEL', meta_key, lease_key)
        table.insert(outcomes, match_id)
        table.insert(outcomes, 'stale')
    elseif type(match.finishedAt) ~= 'number' or match.finishedAt <= 0 then
        redis.call('ZREM', KEYS[1], match_id)
        redis.call('HSET', meta_key, 'state', 'unrecoverable', 'reason', 'finished_at_missing')
        redis.call('EXPIRE', meta_key, 2592000)
        table.insert(outcomes, match_id)
        table.insert(outcomes, 'unrecoverable')
    elseif tonumber(match.finishedAt) + tonumber(ARGV[3]) > tonumber(ARGV[1]) then
        redis.call('ZADD', KEYS[1], tonumber(match.finishedAt) + tonumber(ARGV[3]), match_id)
    elseif redis.call('EXISTS', lease_key) == 1 then
        redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + redis.call('TTL', lease_key), match_id)
    else
        local event = match.matchFinishedEvent
        if not valid_event(match, event, match_id) then
            redis.call('ZREM', KEYS[1], match_id)
            redis.call('HSET', meta_key, 'state', 'unrecoverable', 'reason', 'original_event_missing_or_mismatched')
            redis.call('EXPIRE', meta_key, 2592000)
            table.insert(outcomes, match_id)
            table.insert(outcomes, 'unrecoverable')
        else
            if event_is_queued(event.eventId) then
                redis.call('HSET', meta_key, 'state', 'queued')
                redis.call('EXPIRE', meta_key, 2592000)
                redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[6]), match_id)
                table.insert(outcomes, match_id)
                table.insert(outcomes, 'already_queued')
            else
                local attempts = redis.call('HINCRBY', meta_key, 'attempts', 1)
                redis.call('EXPIRE', meta_key, 2592000)
                if attempts > tonumber(ARGV[4]) then
                    redis.call('ZREM', KEYS[1], match_id)
                    redis.call('HSET', meta_key, 'state', 'exhausted')
                    redis.call('EXPIRE', meta_key, 2592000)
                    table.insert(outcomes, match_id)
                    table.insert(outcomes, 'exhausted')
                else
                    if redis.call('HGET', meta_key, 'state') == 'leased' then
                        table.insert(outcomes, match_id)
                        table.insert(outcomes, 'lease_expired')
                    end
                    local token = ARGV[5] .. ':' .. match_id
                    redis.call('SET', lease_key, token, 'EX', ARGV[6])
                    redis.call('HSET', meta_key, 'state', 'leased', 'lastAttemptAt', ARGV[1])
                    redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[6]), match_id)
                    table.insert(claimed, match_id)
                    table.insert(claimed, token)
                    table.insert(claimed, cjson.encode(event))
                    table.insert(outcomes, match_id)
                    table.insert(outcomes, 'leased')
                end
            end
        end
    end
end
return {claimed, outcomes}
"""

_REEMIT_SAGA_SCRIPT = """
local current = nil
if redis.call('TYPE', KEYS[1]).ok == 'string' then current = redis.call('GET', KEYS[1]) end
local ok, match = pcall(cjson.decode, current or '')
if not ok or type(match) ~= 'table' or match.sagaStatus ~= 'PENDING' then
    redis.call('ZREM', KEYS[4], ARGV[1])
    redis.call('ZREM', KEYS[5], ARGV[1])
    redis.call('DEL', KEYS[2], KEYS[3])
    return 'terminal'
end
if redis.call('GET', KEYS[3]) ~= ARGV[2] then return 'fenced' end
local event = match.matchFinishedEvent
local payload = type(event) == 'table' and event.payload or nil
local expected_count = match.mode == 'VERSUS' and 2 or 1
local valid = type(event) == 'table' and event.eventId == match.sagaEventId
    and match.matchId == ARGV[1] and match.status == 'FINISHED'
    and (match.mode == 'CLASSIC' or match.mode == 'TIMED' or match.mode == 'VERSUS')
    and type(match.caseId) == 'string' and #match.caseId > 0
    and type(match.sagaEventId) == 'string' and #match.sagaEventId > 0
    and type(match.correlationId) == 'string' and #match.correlationId > 0
    and type(event.occurredAt) == 'string'
    and event.correlationId == match.correlationId and type(payload) == 'table'
    and payload.matchId == ARGV[1] and payload.caseId == match.caseId
    and payload.mode == match.mode and type(payload.results) == 'table'
    and type(payload.timeSeconds) == 'number' and payload.timeSeconds >= 0
    and #payload.results == expected_count
if valid then
    for index, result in ipairs(payload.results) do
        local participant = index == 1 and match.player1 or match.player2
        if type(participant) ~= 'string' or #participant == 0
            or type(result) ~= 'table' or result.userId ~= participant
            or type(result.won) ~= 'boolean'
            or (match.mode == 'CLASSIC' and (
                type(result.difficulty) ~= 'string' or #result.difficulty == 0
                or result.xpDelta ~= cjson.null
            ))
            or (match.mode ~= 'CLASSIC' and type(result.xpDelta) ~= 'number') then
            valid = false
            break
        end
    end
end
if not valid then
    redis.call('HSET', KEYS[2], 'state', 'unrecoverable', 'reason', 'original_event_changed')
    redis.call('ZREM', KEYS[4], ARGV[1])
    redis.call('DEL', KEYS[3])
    return 'unrecoverable'
end
local already_queued = false
for _, list_key in ipairs({KEYS[6], KEYS[7]}) do
    local recent = redis.call('LRANGE', list_key, 0, 199)
    for _, queued_raw in ipairs(recent) do
        local queued_ok, queued_event = pcall(cjson.decode, queued_raw)
        if queued_ok and type(queued_event) == 'table' and queued_event.eventId == event.eventId then
            already_queued = true
            break
        end
    end
    if already_queued then break end
end
if not already_queued then
    local raw = cjson.encode(event)
    redis.call('LPUSH', KEYS[6], raw)
    redis.call('HINCRBY', KEYS[2], 'reemissions', 1)
end
redis.call('DEL', KEYS[3])
redis.call('HSET', KEYS[2], 'state', 'pending')
redis.call('ZADD', KEYS[4], ARGV[3], ARGV[1])
return already_queued and 'already_queued' or 'requeued'
"""

_APPLY_SAGA_OUTCOME_SCRIPT = """
local match_key = KEYS[1]
local expected_match = ARGV[1]
if redis.call('TYPE', match_key).ok ~= 'string' then
    return 'retry'
end
if redis.call('GET', match_key) ~= expected_match then
    return 'retry'
end
local with_index = ARGV[#ARGV] == 'INDEX_V1'
local participant_count = with_index and ((#KEYS - 5) / 2) or ((#KEYS - 1) / 2)
if participant_count < 0 or participant_count % 1 ~= 0 then return 'redis_type_error' end
if participant_count > 0 then
    for result_index = 1, participant_count do
        local ranking_key = KEYS[2 + ((result_index - 1) * 2)]
        local history_key = KEYS[3 + ((result_index - 1) * 2)]
        local ranking_type = redis.call('TYPE', ranking_key).ok
        local history_type = redis.call('TYPE', history_key).ok
        if ranking_type ~= 'none' and ranking_type ~= 'zset' then return 'redis_type_error' end
        if history_type ~= 'none' and history_type ~= 'list' then return 'redis_type_error' end
        local argument_index = 4 + ((result_index - 1) * 3)
        local current_score = tonumber(redis.call('ZSCORE', ranking_key, ARGV[argument_index]) or '0')
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
if with_index then
    local index_key = 2 + (participant_count * 2)
    local pending_type = redis.call('TYPE', KEYS[index_key]).ok
    local due_type = redis.call('TYPE', KEYS[index_key + 1]).ok
    if (pending_type ~= 'none' and pending_type ~= 'zset')
        or (due_type ~= 'none' and due_type ~= 'zset') then
        return 'redis_type_error'
    end
end

redis.call('SET', match_key, ARGV[2], 'EX', ARGV[3])
for result_index = 1, participant_count do
    local ranking_key = KEYS[2 + ((result_index - 1) * 2)]
    local history_key = KEYS[3 + ((result_index - 1) * 2)]
    local argument_index = 4 + ((result_index - 1) * 3)
    redis.call('ZINCRBY', ranking_key, ARGV[argument_index + 1], ARGV[argument_index])
    redis.call('LPUSH', history_key, ARGV[argument_index + 2])
end
if with_index then
    local index_key = 2 + (participant_count * 2)
    local old_match = cjson.decode(ARGV[1])
    redis.call('ZREM', KEYS[index_key], old_match.matchId)
    redis.call('ZREM', KEYS[index_key + 1], old_match.matchId)
    redis.call('DEL', KEYS[index_key + 2], KEYS[index_key + 3])
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
                        if type(final_match.get("finishedAt")) is int:
                            finished_at = int(final_match["finishedAt"])
                            pipeline.zadd(PENDING_SAGAS, {match_id: finished_at})
                            pipeline.zadd(SAGA_RECONCILIATION_DUE, {match_id: finished_at})
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
                    if type(match_data.get("finishedAt")) is int:
                        finished_at = int(match_data["finishedAt"])
                        pipeline.zadd(PENDING_SAGAS, {match_id: finished_at})
                        pipeline.zadd(SAGA_RECONCILIATION_DUE, {match_id: finished_at})
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

            keys.extend([
                PENDING_SAGAS,
                SAGA_RECONCILIATION_DUE,
                f"matches:saga:reconcile:{match_id}",
                f"matches:saga:reconcile:{match_id}:lease",
            ])
            arguments.append("INDEX_V1")
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

    def rebuild_pending_saga_index(self, scan_count: int = 500) -> int:
        """Backfill the derived pending-saga index once without blocking Redis."""
        if self.client.exists(SAGA_RECONCILIATION_INDEX_READY):
            return 0
        lock_key = f"{SAGA_RECONCILIATION_INDEX_READY}:lock"
        if not self.client.set(lock_key, "1", nx=True, ex=300):
            return 0
        indexed = 0
        try:
            cursor = 0
            while True:
                cursor, keys = self.client.scan(cursor=cursor, match="match:*", count=scan_count)
                for key in keys:
                    if not key.startswith("match:") or ":" in key[len("match:"):]:
                        continue
                    match_id = key[len("match:"):]
                    with self.client.pipeline() as pipeline:
                        try:
                            pipeline.watch(key)
                            raw = pipeline.get(key)
                            if not raw:
                                pipeline.unwatch()
                                continue
                            match = json.loads(raw)
                            if (
                                match.get("matchId") != match_id
                                or match.get("sagaStatus") != "PENDING"
                                or type(match.get("finishedAt")) is not int
                            ):
                                pipeline.unwatch()
                                continue
                            finished_at = match["finishedAt"]
                            pipeline.multi()
                            pipeline.zadd(PENDING_SAGAS, {match_id: finished_at})
                            pipeline.zadd(SAGA_RECONCILIATION_DUE, {match_id: finished_at})
                            pipeline.execute()
                            indexed += 1
                        except (redis.WatchError, TypeError, ValueError, AttributeError):
                            continue
                if cursor == 0:
                    break
            self.client.set(SAGA_RECONCILIATION_INDEX_READY, "1")
            return indexed
        finally:
            self.client.delete(lock_key)

    def claim_pending_sagas(
        self,
        *,
        now: int,
        limit: int,
        min_age_seconds: int,
        max_attempts: int,
        lease_seconds: int,
        token_prefix: str,
    ) -> tuple[list[tuple[str, str, dict[str, Any]]], list[tuple[str, str]]]:
        values = self.client.eval(
            _CLAIM_SAGAS_SCRIPT,
            2,
            SAGA_RECONCILIATION_DUE,
            PENDING_SAGAS,
            str(now),
            str(limit),
            str(min_age_seconds),
            str(max_attempts),
            token_prefix,
            str(lease_seconds),
        )
        return _decode_saga_claim_response(values)

    def oldest_pending_saga_finished_at(self) -> Optional[int]:
        oldest = self.client.zrange(PENDING_SAGAS, 0, 0, withscores=True)
        return int(oldest[0][1]) if oldest else None

    def reemit_pending_saga(
        self, match_id: str, token: str, *, next_attempt_at: int
    ) -> str:
        result = self.client.eval(
            _REEMIT_SAGA_SCRIPT,
            7,
            f"match:{match_id}",
            f"matches:saga:reconcile:{match_id}",
            f"matches:saga:reconcile:{match_id}:lease",
            SAGA_RECONCILIATION_DUE,
            PENDING_SAGAS,
            MATCH_EVENT_OUTBOX,
            MATCH_EVENT_INFLIGHT,
            match_id,
            token,
            str(next_attempt_at),
        )
        return result

    def save_progress(self, match_id: str, user_id: str, progress: float) -> None:
        self.client.hset(f"match:{match_id}:progress", user_id, progress)

    def publish_progress(self, match_id: str, user_id: str, progress: float) -> None:
        self.client.publish(
            f"match:{match_id}:progress:events",
            json.dumps({"userId": user_id, "progress": progress}),
        )

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
