from datetime import UTC, datetime

from app.config import Settings
from app.policy import policy_phrase_present
from app.workflows import next_local_window

SETTINGS = Settings(environment="test")


def test_contact_window_closing_boundary_moves_to_next_day():
    at_close = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)  # 18:00 New York
    scheduled = next_local_window(at_close, "America/New_York", SETTINGS)
    assert scheduled == datetime(2026, 8, 18, 13, 0, tzinfo=UTC)


def test_contact_window_respects_daylight_saving_transition():
    before_open_after_dst = datetime(2026, 3, 8, 12, 30, tzinfo=UTC)  # Sunday 08:30 EDT
    scheduled = next_local_window(before_open_after_dst, "America/New_York", SETTINGS)
    assert scheduled == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def test_invalid_timezone_falls_back_to_utc_window():
    before_open = datetime(2026, 8, 17, 8, 30, tzinfo=UTC)
    scheduled = next_local_window(before_open, "Not/A_Zone", SETTINGS)
    assert scheduled == datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def test_policy_phrase_matching_canonicalizes_generated_text():
    assert policy_phrase_present("guaranteed sales", "We promise GUARANTEED—SALES for sponsors")
    assert policy_phrase_present("legal terms", "Questions about legal, terms should be escalated")
    assert not policy_phrase_present("sales", "Our salesforce integration is available")
