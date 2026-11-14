from app.domain.timed_match import (
    FINISHED,
    IN_PROGRESS,
    TimedOutcome,
    calculate_outcome,
    deadline_has_expired,
    elapsed_seconds,
    finish_match_data,
)


def test_elapsed_time_and_deadline_boundary_are_preserved():
    assert elapsed_seconds(1_000, 1_025) == 25
    assert deadline_has_expired(1_025, 1_025) is False
    assert deadline_has_expired(1_025, 1_026) is True


def test_correct_solution_at_deadline_wins_without_bonus():
    outcome = calculate_outcome(100, 160, 160, True)

    assert outcome == TimedOutcome(won=True, time_seconds=60, bonus=0, xp_delta=50)


def test_correct_solution_before_deadline_uses_two_xp_per_remaining_second():
    outcome = calculate_outcome(100, 160, 150, True)

    assert outcome == TimedOutcome(won=True, time_seconds=50, bonus=20, xp_delta=70)


def test_wrong_solution_and_expired_deadline_are_losses():
    assert calculate_outcome(100, 160, 150, False) == TimedOutcome(
        won=False,
        time_seconds=50,
        bonus=0,
        xp_delta=-10,
    )
    assert calculate_outcome(100, 160, 161, True) == TimedOutcome(
        won=False,
        time_seconds=61,
        bonus=0,
        xp_delta=-10,
    )


def test_finished_match_data_keeps_existing_fields_and_sets_final_state():
    original = {"status": IN_PROGRESS, "caseId": "case-1", "extra": "kept"}
    outcome = TimedOutcome(won=True, time_seconds=3, bonus=4, xp_delta=54)

    finished = finish_match_data(original, outcome)

    assert original["status"] == IN_PROGRESS
    assert finished == {
        "status": FINISHED,
        "caseId": "case-1",
        "extra": "kept",
        "finalResult": {
            "won": True,
            "timeSeconds": 3,
            "bonus": 4,
            "xpDelta": 54,
        },
    }
