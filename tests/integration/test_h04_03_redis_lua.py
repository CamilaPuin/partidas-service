import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from app.repositories.matches import (
    FINISHED_MATCH_TTL_SECONDS,
    MatchRepository,
    _APPLY_SAGA_OUTCOME_SCRIPT,
)


@pytest.fixture
def isolated_redis_case():
    host = os.getenv("H04_TEST_REDIS_HOST")
    if not host:
        pytest.skip("requires the isolated H04-03 Redis Compose service")
    client = redis.Redis(
        host=host,
        port=int(os.getenv("H04_TEST_REDIS_PORT", "6379")),
        db=0,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )
    client.ping()

    suffix = uuid.uuid4().hex
    match_id = f"h04-versus-{suffix}"
    correlation_id = f"corr-{suffix}"
    event_id = f"event-{suffix}"
    player1 = f"player-a-{suffix}"
    player2 = f"player-b-{suffix}"
    match_key = f"match:{match_id}"
    history_keys = [f"user:{player1}:history", f"user:{player2}:history"]
    users = [player1, player2]
    results = [
        {"userId": player1, "won": True, "xpDelta": 100, "firstWinRewarded": False},
        {"userId": player2, "won": False, "xpDelta": 0, "firstWinRewarded": False},
    ]
    match = {
        "matchId": match_id,
        "mode": "VERSUS",
        "status": "FINISHED",
        "caseId": "case-h04-test",
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
                "caseId": "case-h04-test",
                "results": [
                    {"userId": player1, "won": True, "xpDelta": 100, "difficulty": None},
                    {"userId": player2, "won": False, "xpDelta": 0, "difficulty": None},
                ],
            },
        },
    }
    client.set(match_key, json.dumps(match))
    yield {
        "client": client,
        "match": match,
        "match_id": match_id,
        "match_key": match_key,
        "correlation_id": correlation_id,
        "users": users,
        "history_keys": history_keys,
        "results": results,
        "unknown_user": f"stranger-{suffix}",
    }

    client.delete(match_key, *history_keys)
    client.zrem("ranking:global", *users)
    client.close()


def snapshot(case, observed_users=None):
    client = case["client"]
    users = observed_users or case["users"]
    history_keys = {user: f"user:{user}:history" for user in users}
    history_types = {user: client.type(key) for user, key in history_keys.items()}
    histories = {}
    for user, key in history_keys.items():
        if history_types[user] == "list":
            histories[user] = client.lrange(key, 0, -1)
        elif history_types[user] == "string":
            histories[user] = client.get(key)
        else:
            histories[user] = None
    return {
        "match": client.get(case["match_key"]),
        "ranking": {user: client.zscore("ranking:global", user) for user in users},
        "history_types": history_types,
        "history": histories,
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "missing-player-a",
        "missing-player-b",
        "empty-results",
        "duplicate-player",
        "unknown-player",
        "extra-player",
        "contradictory-result",
        "wrong-match-id",
        "wrong-mode",
    ],
)
def test_real_redis_rejects_invalid_versus_without_side_effects(
    isolated_redis_case, scenario
):
    case = isolated_redis_case
    match = case["match"]
    results = [dict(item) for item in case["results"]]
    if scenario == "missing-player-a":
        results.pop(0)
    elif scenario == "missing-player-b":
        results.pop()
    elif scenario == "empty-results":
        results.clear()
    elif scenario == "duplicate-player":
        results[1]["userId"] = results[0]["userId"]
    elif scenario == "unknown-player":
        results[1]["userId"] = case["unknown_user"]
    elif scenario == "extra-player":
        results.append({"userId": case["unknown_user"], "won": False, "xpDelta": 0})
    elif scenario == "contradictory-result":
        results[1]["won"] = True
    elif scenario == "wrong-match-id":
        match["matchId"] = "different-match"
    elif scenario == "wrong-mode":
        match["mode"] = "CLASSIC"

    case["client"].set(case["match_key"], json.dumps(match))
    watched_users = [*case["users"], case["unknown_user"]]
    before = snapshot(case, watched_users)

    result = MatchRepository(case["client"]).apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", results
    )

    assert result == "invalid"
    assert snapshot(case, watched_users) == before
    assert json.loads(before["match"])["sagaStatus"] == "PENDING"


def test_real_redis_confirms_complete_versus_and_repeat_is_idempotent(isolated_redis_case):
    case = isolated_redis_case
    repository = MatchRepository(case["client"])

    assert repository.apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", case["results"]
    ) == "confirmed"
    after_first = snapshot(case)
    assert json.loads(after_first["match"])["sagaStatus"] == "CONFIRMED"
    assert after_first["ranking"] == {case["users"][0]: 100.0, case["users"][1]: 0.0}
    assert len(after_first["history"][case["users"][0]]) == 1
    assert len(after_first["history"][case["users"][1]]) == 1

    assert repository.apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", case["results"]
    ) == "duplicate"
    assert snapshot(case) == after_first


def test_real_redis_rejects_contradictory_repeat_without_side_effects(isolated_redis_case):
    case = isolated_redis_case
    repository = MatchRepository(case["client"])
    assert repository.apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", case["results"]
    ) == "confirmed"
    before = snapshot(case)
    contradictory = [dict(item) for item in case["results"]]
    contradictory[0]["xpDelta"] = 99

    assert repository.apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", contradictory
    ) == "conflict"
    assert snapshot(case) == before


def test_real_redis_valid_confirmation_succeeds_after_rejected_one(isolated_redis_case):
    case = isolated_redis_case
    repository = MatchRepository(case["client"])

    assert repository.apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", case["results"][:1]
    ) == "invalid"
    assert json.loads(case["client"].get(case["match_key"]))["sagaStatus"] == "PENDING"
    assert case["client"].zcard("ranking:global") == 0
    assert case["client"].exists(*case["history_keys"]) == 0

    assert repository.apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", case["results"]
    ) == "confirmed"
    assert json.loads(case["client"].get(case["match_key"]))["sagaStatus"] == "CONFIRMED"


def test_real_redis_wrong_history_type_does_not_partially_apply(isolated_redis_case):
    case = isolated_redis_case
    client = case["client"]
    client.set(case["history_keys"][1], "deliberately-wrong-type")
    before = snapshot(case)

    result = MatchRepository(client).apply_saga_outcome(
        case["match_id"], case["correlation_id"], "CONFIRMED", case["results"]
    )

    assert result == "invalid"
    assert snapshot(case) == before
    assert client.get(case["history_keys"][1]) == "deliberately-wrong-type"


def test_real_redis_rejects_stale_match_snapshot_before_writing(isolated_redis_case):
    case = isolated_redis_case
    client = case["client"]
    stale_match = client.get(case["match_key"])
    current_match = dict(case["match"], revision=2)
    client.set(case["match_key"], json.dumps(current_match))
    updated_match = dict(current_match, sagaStatus="CONFIRMED")
    history = json.dumps({"matchId": case["match_id"], "result": "WON"})
    keys = [case["match_key"], "ranking:global", *case["history_keys"]]
    arguments = [
        stale_match,
        json.dumps(updated_match),
        str(FINISHED_MATCH_TTL_SECONDS),
        case["users"][0],
        "100",
        history,
        case["users"][1],
        "0",
        history,
    ]
    before = snapshot(case)

    assert client.eval(_APPLY_SAGA_OUTCOME_SCRIPT, len(keys), *keys, *arguments) == "retry"
    assert snapshot(case) == before
    assert json.loads(before["match"])["sagaStatus"] == "PENDING"


def test_real_redis_concurrent_confirmations_apply_rewards_once(isolated_redis_case):
    case = isolated_redis_case

    def confirm():
        return MatchRepository(case["client"]).apply_saga_outcome(
            case["match_id"], case["correlation_id"], "CONFIRMED", case["results"]
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _index: confirm(), range(8)))

    assert outcomes.count("confirmed") == 1
    assert outcomes.count("duplicate") == 7
    state = snapshot(case)
    assert json.loads(state["match"])["sagaStatus"] == "CONFIRMED"
    assert state["ranking"] == {case["users"][0]: 100.0, case["users"][1]: 0.0}
    assert all(len(state["history"][user]) == 1 for user in case["users"])
