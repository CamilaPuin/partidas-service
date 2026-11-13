from app.application.ports import AppliedUserResult
from app.infrastructure.adapters import timed_match


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
