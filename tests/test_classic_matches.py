import pytest

from app.application.errors import (
    MatchConflict,
    OwnMatch,
    ServiceIntegrationError,
    UserResultConflict,
)
from app.application.matches import MatchService
from app.application.ports import AppliedUserResult


USER_ID = "603fba8d-bb2c-4994-bc27-e9c40e30fec8"
MATCH_ID = "match-1"
ATTEMPT_ID = "ac5ef82c-2747-4bfc-bccd-fd6e0d12d03f"
POSITIONS = [
    {"characterId": "V1", "x": 0, "y": 0},
    {"characterId": "S1", "x": 1, "y": 0},
]


class FakeCases:
    def __init__(self):
        self.difficulty = "MEDIUM"
        self.valid = False
        self.error = None
        self.validations = []

    def get_difficulty(self, case_id):
        if self.error:
            raise self.error
        self.difficulty_calls = getattr(self, "difficulty_calls", []) + [case_id]
        return self.difficulty

    def validate(self, case_id, positions):
        self.validations.append((case_id, positions))
        if self.error:
            raise self.error
        return self.valid


class FakeUsers:
    def __init__(self):
        self.level = 10
        self.error = None
        self.register_error = None
        self.result = {"xpDelta": 100, "firstWinRewarded": True}
        self.registered = []

    def get_progression(self, user_id):
        if self.error:
            raise self.error
        return {"level": self.level}

    def register(self, *args):
        self.registered.append(args)
        if self.register_error:
            raise self.register_error
        return AppliedUserResult(
            xp_delta=self.result["xpDelta"],
            first_win_rewarded=self.result["firstWinRewarded"],
        )


class FixedClock:
    def __init__(self, *values):
        self.values = iter(values or (1234,))

    def now_seconds(self):
        return next(self.values)


class FixedIds:
    def new_id(self):
        return MATCH_ID


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
    service = MatchService(
        store=repository,
        cases=FakeCases(),
        users=FakeUsers(),
        clock=FixedClock(160, 160, 160),
        ids=FixedIds(),
    )
    return service, repository


def test_classic_match_uses_server_case_difficulty_and_stores_snapshot():
    repository = InMemoryMatchRepository()
    cases = FakeCases()
    cases.difficulty = "HARD"
    service = MatchService(
        store=repository,
        cases=cases,
        users=FakeUsers(),
        clock=FixedClock(1234),
        ids=FixedIds(),
    )

    created = service.create_classic_match(
        "case-1",
        USER_ID,
    )

    stored = repository.get_match(created["matchId"])
    assert created["caseId"] == "case-1"
    assert created["status"] == "IN_PROGRESS"
    assert stored["difficulty"] == "HARD"
    assert stored["startedAt"] == 1234
    assert stored["mode"] == "CLASSIC"
    assert stored["player1"] == USER_ID


def test_classic_request_does_not_accept_client_difficulty():
    from app.schemas.matches import ClassicMatchRequest

    with pytest.raises(ValueError):
        ClassicMatchRequest(caseId="case-1", difficulty="EASY")


def test_classic_attempt_requires_uuid_and_unique_character_ids():
    from app.schemas.matches import ClassicAttemptRequest, PositionDTO, SolutionDTO

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
    with pytest.raises(ValueError):
        SolutionDTO(
            positions=[
                PositionDTO(**POSITIONS[0]),
                PositionDTO(characterId="V1", x=1, y=0),
            ]
        )


def test_incorrect_classic_attempt_remains_open_and_replays_idempotently():
    service, repository = build_match()
    service.cases.valid = False

    first = service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)
    second = service.attempt_classic_match(
        MATCH_ID, USER_ID, ATTEMPT_ID, list(reversed(POSITIONS))
    )

    assert first["valid"] is False
    assert first["status"] == "IN_PROGRESS"
    assert second == first
    assert len(service.cases.validations) == 1
    assert not service.users.registered
    assert repository.get_match(MATCH_ID)["status"] == "IN_PROGRESS"


def test_reusing_attempt_id_with_different_positions_conflicts():
    service, _repository = build_match()

    service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)
    changed = [{"characterId": "V1", "x": 2, "y": 0}]

    with pytest.raises(MatchConflict):
        service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, changed)


def test_classic_success_uses_applied_xp_from_users_service():
    service, repository = build_match()
    service.cases.valid = True
    result = service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)
    replay = service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)

    assert result["valid"] is True
    assert replay == result
    assert result["status"] == "FINISHED"
    assert result["xpDelta"] == 100
    assert result["firstWinRewarded"] is True
    assert service.users.registered == [(
        USER_ID,
        MATCH_ID,
        "case-1",
        "CLASSIC",
        True,
        60,
        None,
        "MEDIUM",
    )]
    assert repository.get_match(MATCH_ID)["status"] == "FINISHED"
    assert repository.scores[USER_ID] == 100
    assert repository.history[USER_ID][0]["result"] == "WON"
    assert len(repository.history[USER_ID]) == 1


def test_retry_recovers_finalizing_classic_result_without_revalidation():
    service, repository = build_match()
    registrations = []
    service.clock = FixedClock(160, 300)
    service.cases.valid = True

    def register(*args):
        registrations.append(args)
        if len(registrations) == 1:
            raise ServiceIntegrationError("resultado temporalmente no disponible")
        return AppliedUserResult(xp_delta=100, first_win_rewarded=True)

    service.users.register = register

    with pytest.raises(ServiceIntegrationError):
        service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)
    assert repository.get_match(MATCH_ID)["status"] == "FINALIZING"

    result = service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)

    assert result["xpDelta"] == 100
    assert result["firstWinRewarded"] is True
    assert len(service.cases.validations) == 1
    assert len(registrations) == 2
    assert registrations[0][5] == registrations[1][5] == 60


def test_contradictory_users_result_becomes_match_conflict_and_keeps_finalizing(
):
    service, repository = build_match()
    service.cases.valid = True

    def conflict(*_args):
        raise UserResultConflict("matchId contradictorio")

    service.users.register = conflict

    with pytest.raises(MatchConflict):
        service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)

    assert repository.get_match(MATCH_ID)["status"] == "FINALIZING"


def test_validation_error_releases_attempt_reservation():
    service, repository = build_match()
    service.cases.error = ServiceIntegrationError("cases-service no disponible")

    with pytest.raises(ServiceIntegrationError):
        service.attempt_classic_match(MATCH_ID, USER_ID, ATTEMPT_ID, POSITIONS)

    match = repository.get_match(MATCH_ID)
    assert match["status"] == "IN_PROGRESS"
    assert "activeAttempt" not in match


def test_classic_attempt_requires_the_match_owner():
    service, _repository = build_match()

    with pytest.raises(OwnMatch):
        service.attempt_classic_match(MATCH_ID, "another-user", ATTEMPT_ID, POSITIONS)
    assert service.cases.validations == []
