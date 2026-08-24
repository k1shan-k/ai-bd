from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.timing import (
    conversation_reply_time,
    humanized_outreach_time,
    next_local_window,
    stable_int,
)

SETTINGS = Settings(_env_file=None, environment="test")


def test_stable_jitter_is_retry_reproducible_and_varies_by_action():
    first = stable_int("lead-a:initial-email", 240, 1680)
    assert first == stable_int("lead-a:initial-email", 240, 1680)
    values = {stable_int(f"lead-{index}:initial-email", 240, 1680) for index in range(8)}
    assert len(values) > 1


def test_outreach_rolls_weekend_to_prospect_local_monday():
    friday_after_close = datetime(2026, 8, 21, 23, 30, tzinfo=UTC)  # 19:30 EDT
    due = humanized_outreach_time(
        friday_after_close,
        "America/New_York",
        SETTINGS,
        seed="weekend-lead",
    )
    assert datetime(2026, 8, 24, 13, 4, tzinfo=UTC) <= due
    assert due <= datetime(2026, 8, 24, 13, 28, tzinfo=UTC)


def test_weekend_window_uses_post_dst_local_offset():
    sunday_before_spring_dst = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    due = next_local_window(sunday_before_spring_dst, "America/New_York", SETTINGS)
    assert due == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)  # Monday 09:00 EDT


def test_reply_delay_reflects_urgency_and_message_complexity():
    now = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    urgent = conversation_reply_time(
        now,
        "UTC",
        SETTINGS,
        seed="urgent",
        intent="call_request",
        body="Can we book a call?",
    )
    complex_objection = conversation_reply_time(
        now,
        "UTC",
        SETTINGS,
        seed="complex",
        intent="objection",
        body=(
            "The package may be over budget. Does the booth include power? "
            "Can legal review the terms, and what audience segment attends?"
        ),
    )
    assert timedelta(seconds=45) <= urgent - now <= timedelta(seconds=120)
    assert complex_objection - now >= timedelta(seconds=240)
    assert urgent < complex_objection


def test_reply_near_friday_close_rolls_to_monday_business_hours():
    friday_close = datetime(2026, 8, 21, 17, 59, tzinfo=UTC)
    due = conversation_reply_time(
        friday_close,
        "UTC",
        SETTINGS,
        seed="late-question",
        intent="question",
        body="Could you share the package details?",
    )
    assert datetime(2026, 8, 24, 9, 0, tzinfo=UTC) <= due
    assert due < datetime(2026, 8, 24, 9, 10, tzinfo=UTC)
