import pytest

from app.core.service_clients import ServiceIntegrationError
from app.schemas.matches import (
    ClassicAttemptRequest,
    ClassicMatchRequest,
    PositionDTO,
)
from app.services import matches as match_module
from app.services.matches import MatchConflict, MatchService, OwnMatch


USER_ID = "603fba8d-bb2c-4994-bc27-e9c40e30fec8"
MATCH_ID = "match-1"
ATTEMPT_ID = "ac5ef82c-2747-4bfc-bccd-fd6e0d12d03f"
POSITIONS = [
    {"characterId": "V1", "x": 0, "y": 0},
    {"characterId": "S1", "x": 1, "y": 0},
]


class InMemoryMatchRepository:
    def __init__(self):
        self.matches = {}
        self.attempts = {}
        self.scores = {}
        self.history = {}

    def get_match(self, match_id):
        return self.matches.get(match_id)

    def save_match(self, match_id, match_data):
        self.matches[match_id] = dict(match_data)

    def reserve_classic_attempt(self, match_id, attempt_id, fingerprint):
        match = self.matches.get(match_id)
        if not match:
            return {"action": "not_found"}
        attempt = self.attempts.get((match_id, attempt_id))
        if attempt:
            if attempt["fingerprint"] != fingerprint:
                return {"action": "conflict"}
            return {"action": "replay", "result": attempt["result"]}
        active = match.get("activeAttempt")
        if active:
            if (
                active["attemptId"] != attempt_id
                or active["fingerprint"] != fingerprint
            ):
                return {"action": "conflict"}
            return {
                "action": "finalize" if match["status"] == "FINALIZING" else "validate",
                "match": match,
            }
        if match["status"] != "IN_PROGRESS":
            return {"action": "unavailable"}
        match["status"] = "VALIDATING"
        match["activeAttempt"] = {
            "attemptId": attempt_id,
            "fingerprint": fingerprint,
        }
        return {"action": "validate", "match": match}

    def release_classic_validation(self, match_id, attempt_id, fingerprint):
        match = self.matches[match_id]
        if match["activeAttempt"]["attemptId"] != attempt_id:
            return False
        match["status"] = "IN_PROGRESS"
        match.pop("activeAttempt")
        return True

    def mark_classic_finalizing(
        self,
        match_id,
        attempt_id,
        fingerprint,
        time_seconds,
        finalized_at,
    ):
        match = self.matches[match_id]
        match["status"] = "FINALIZING"
        match["finalizingTimeSeconds"] = time_seconds
        match["finalizingAt"] = finalized_at
        return {"action": "finalize", "match": match}

    def finish_classic_incorrect_attempt(
        self, match_id, attempt_id, fingerprint, result
    ):
        match = self.matches[match_id]
        match["status"] = "IN_PROGRESS"
        match.pop("activeAttempt")
        self.attempts[(match_id, attempt_id)] = {
            "fingerprint": fingerprint,
            "result": result,
        }
        return {"action": "stored", "result": result}

    def complete_classic_attempt(
        self,
        match_id,
        attempt_id,
        fingerprint,
        match_data,
        result,
        user_id,
        score_delta,
        history_entry,
    ):
        match = self.matches[match_id]
        match["status"] = "FINISHED"
        match["finalResult"] = result
        match.pop("activeAttempt")
        self.attempts[(match_id, attempt_id)] = {
            "fingerprint": fingerprint,
            "result": result,
        }
        self.scores[user_id] = self.scores.get(user_id, 0) + score_delta
        self.history.setdefault(user_id, []).append(history_entry)
        return {"action": "stored", "result": result}


def build_match(repository=None):
    repository = repository or InMemoryMatchRepository()
    repository.save_match(
        MATCH_ID,
        {
            "matchId": MATCH_ID,
            "caseId": "case-1",
            "difficulty": "MEDIUM",
            "startedAt": 100,
            "mode": "CLASSIC",
            "status": "IN_PROGRESS",
            "player1": USER_ID,
        },
    )
    return MatchService(repository), repository


def build_attempt(positions=POSITIONS):
    return ClassicAttemptRequest(
        attemptId=ATTEMPT_ID,
        positions=[PositionDTO(**position) for position in positions],
    )


def test_classic_match_uses_server_case_difficulty_and_stores_snapshot(monkeypatch):
    repository = InMemoryMatchRepository()
    service = MatchService(repository)
    monkeypatch.setattr(match_module, "get_case_difficulty", lambda _case_id: "HARD")
    monkeypatch.setattr(match_module.time, "time", lambda: 1234)

    created = service.create_classic_match(
        ClassicMatchRequest(caseId="case-1"),
        USER_ID,
    )

    stored = repository.get_match(created.matchId)
    assert created.caseId == "case-1"
    assert created.status == "IN_PROGRESS"
    assert stored["difficulty"] == "HARD"
    assert stored["startedAt"] == 1234
    assert stored["mode"] == "CLASSIC"
    assert stored["player1"] == USER_ID


def test_classic_request_does_not_accept_client_difficulty():
    with pytest.raises(ValueError):
        ClassicMatchRequest(caseId="case-1", difficulty="EASY")


def test_classic_attempt_requires_uuid_and_unique_character_ids():
    with pytest.raises(ValueError):
        ClassicAttemptRequest(
            attemptId="not-a-uuid",
            positions=[PositionDTO(**POSITIONS[0])],
        )
    with pytest.raises(ValueError):
        ClassicAttemptRequest(
            attemptId=ATTEMPT_ID,
            positions=[
                PositionDTO(**POSITIONS[0]),
                PositionDTO(characterId="V1", x=1, y=0),
            ],
        )


def test_incorrect_classic_attempt_remains_open_and_replays_idempotently(monkeypatch):
    service, repository = build_match()
    validations = []
    monkeypatch.setattr(
        match_module,
        "validate_case",
        lambda _case_id, _positions: validations.append(True) or False,
    )
    monkeypatch.setattr(
        match_module,
        "register_user_result",
        lambda *_args, **_kwargs: pytest.fail("No debe registrar intentos incorrectos"),
    )

    first = service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt())
    second = service.attempt_classic_match(
        MATCH_ID,
        USER_ID,
        build_attempt(list(reversed(POSITIONS))),
    )

    assert first.valid is False
    assert first.status == "IN_PROGRESS"
    assert second == first
    assert len(validations) == 1
    assert repository.get_match(MATCH_ID)["status"] == "IN_PROGRESS"


def test_reusing_attempt_id_with_different_positions_conflicts(monkeypatch):
    service, _repository = build_match()
    monkeypatch.setattr(match_module, "validate_case", lambda *_args: False)

    service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt())
    changed = [{"characterId": "V1", "x": 2, "y": 0}]

    with pytest.raises(MatchConflict):
        service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt(changed))


def test_classic_success_uses_applied_xp_from_users_service(monkeypatch):
    service, repository = build_match()
    monkeypatch.setattr(match_module, "validate_case", lambda *_args: True)
    monkeypatch.setattr(match_module.time, "time", lambda: 160)
    registered = {}
    registered_calls = []

    def register_result(*args):
        registered["args"] = args
        registered_calls.append(args)
        return {"xpDelta": 100, "firstWinRewarded": True}

    monkeypatch.setattr(match_module, "register_user_result", register_result)

    result = service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt())
    replay = service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt())

    assert result.valid is True
    assert replay == result
    assert result.status == "FINISHED"
    assert result.xpDelta == 100
    assert result.firstWinRewarded is True
    assert registered["args"] == (
        USER_ID,
        MATCH_ID,
        "case-1",
        "CLASSIC",
        True,
        60,
        None,
        "MEDIUM",
    )
    assert repository.get_match(MATCH_ID)["status"] == "FINISHED"
    assert repository.scores[USER_ID] == 100
    assert repository.history[USER_ID][0]["result"] == "WON"
    assert len(registered_calls) == 1
    assert len(repository.history[USER_ID]) == 1


def test_retry_recovers_finalizing_classic_result_without_revalidation(monkeypatch):
    service, repository = build_match()
    validations = []
    registrations = []
    times = iter([160, 300])
    registration_args = []
    monkeypatch.setattr(match_module.time, "time", lambda: next(times))

    def validate(*_args):
        validations.append(True)
        return True

    def register(*args):
        registrations.append(True)
        registration_args.append(args)
        if len(registrations) == 1:
            raise ServiceIntegrationError("resultado temporalmente no disponible")
        return {"xpDelta": 100, "firstWinRewarded": True}

    monkeypatch.setattr(match_module, "validate_case", validate)
    monkeypatch.setattr(match_module, "register_user_result", register)

    with pytest.raises(ServiceIntegrationError):
        service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt())
    assert repository.get_match(MATCH_ID)["status"] == "FINALIZING"

    result = service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt())

    assert result.xpDelta == 100
    assert result.firstWinRewarded is True
    assert len(validations) == 1
    assert len(registrations) == 2
    assert registration_args[0][5] == registration_args[1][5] == 60


def test_validation_error_releases_attempt_reservation(monkeypatch):
    service, repository = build_match()

    def unavailable(*_args):
        raise ServiceIntegrationError("cases-service no disponible")

    monkeypatch.setattr(match_module, "validate_case", unavailable)

    with pytest.raises(ServiceIntegrationError):
        service.attempt_classic_match(MATCH_ID, USER_ID, build_attempt())

    match = repository.get_match(MATCH_ID)
    assert match["status"] == "IN_PROGRESS"
    assert "activeAttempt" not in match


def test_classic_attempt_requires_the_match_owner(monkeypatch):
    service, _repository = build_match()
    monkeypatch.setattr(
        match_module,
        "validate_case",
        lambda *_args: pytest.fail("No debe validar para otro usuario"),
    )

    with pytest.raises(OwnMatch):
        service.attempt_classic_match(MATCH_ID, "another-user", build_attempt())
