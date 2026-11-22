import json

import pytest

from app.repositories.matches import (
    MatchRepository,
    SagaReconciliationResponseError,
    _decode_saga_claim_response,
)


class ScriptReplyRedis:
    def __init__(self, reply):
        self.reply = reply

    def eval(self, *_args):
        return self.reply


def test_claim_parser_matches_nested_redis_eval_response_with_bytes():
    event = {"eventId": "event-1", "correlationId": "corr-1", "payload": {"matchId": "match-1"}}
    reply = [
        [b"match-1", b"worker:match-1", json.dumps(event).encode("utf-8")],
        [b"match-1", b"leased", b"match-2", b"exhausted"],
    ]

    assert _decode_saga_claim_response(reply) == (
        [("match-1", "worker:match-1", event)],
        [("match-1", "leased"), ("match-2", "exhausted")],
    )


def test_repository_claim_method_consumes_nested_reply_without_flat_indexing():
    event = {"eventId": "event-1", "payload": {"matchId": "match-1"}}
    redis_client = ScriptReplyRedis([
        ["match-1", "worker:match-1", json.dumps(event)],
        ["match-1", "leased"],
    ])

    assert MatchRepository(redis_client).claim_pending_sagas(
        now=100,
        limit=10,
        min_age_seconds=1,
        max_attempts=3,
        lease_seconds=30,
        token_prefix="worker",
    ) == ([('match-1', 'worker:match-1', event)], [('match-1', 'leased')])


def test_claim_parser_accepts_empty_nested_arrays_when_no_saga_is_eligible():
    assert _decode_saga_claim_response([[], []]) == ([], [])


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        [b"match-1", b"token", b"{}"],
        [[b"match-1", b"token"], []],
        [[], [b"match-1"]],
        [[b"match-1", b"token", b"not-json"], []],
        [[], [b"match-1", b"not-a-known-outcome"]],
        [[b"match-1", b"\xff", b"{}"], []],
    ],
)
def test_claim_parser_rejects_malformed_or_inconsistent_reply(reply):
    with pytest.raises(SagaReconciliationResponseError):
        _decode_saga_claim_response(reply)


def test_repository_surfaces_malformed_lua_reply_instead_of_returning_empty_claims():
    repository = MatchRepository(ScriptReplyRedis([[b"match-1"], []]))

    with pytest.raises(SagaReconciliationResponseError, match="claim fields"):
        repository.claim_pending_sagas(
            now=100,
            limit=10,
            min_age_seconds=1,
            max_attempts=3,
            lease_seconds=30,
            token_prefix="worker",
        )
