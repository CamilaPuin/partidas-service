from app.application.ports import AppliedUserResult
from app.infrastructure.adapters import timed_match


def test_cases_adapter_delegates_to_real_cases_validation(monkeypatch):
    seen = {}

    def fake_validate(case_id, positions):
        seen["case_id"] = case_id
        seen["positions"] = positions
        return True

    monkeypatch.setattr(timed_match, "validate_case", fake_validate)
    adapter = timed_match.CasesHttpAdapter()
    positions = [{"characterId": "S1", "x": 0, "y": 0}]

    assert adapter.validate("case-1", positions) is True
    assert seen == {"case_id": "case-1", "positions": positions}


def test_users_adapter_sends_match_id_without_difficulty_and_returns_applied_result(
    monkeypatch,
):
    seen = {}

    def fake_register(*args):
        seen["args"] = args
        return {"xpDelta": 11, "firstWinRewarded": False}

    monkeypatch.setattr(timed_match, "register_user_result", fake_register)
    adapter = timed_match.UsersHttpAdapter()

    result = adapter.register("user-1", "match-1", "case-1", "TIMED", True, 20, 30)

    assert seen["args"] == (
        "user-1",
        "match-1",
        "case-1",
        "TIMED",
        True,
        20,
        30,
        None,
    )
    assert result == AppliedUserResult(xp_delta=11, first_win_rewarded=False)
