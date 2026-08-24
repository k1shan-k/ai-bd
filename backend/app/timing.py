from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def timezone_for(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def stable_int(seed: str, minimum: int, maximum: int) -> int:
    """Return a retry-stable value without global RNG state."""
    if minimum > maximum:
        raise ValueError("minimum must not exceed maximum")
    width = maximum - minimum + 1
    value = int.from_bytes(sha256(seed.encode("utf-8")).digest()[:8], "big")
    return minimum + value % width


def _next_weekday(value: date) -> date:
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def _clamp_to_business_window(local: datetime, settings: Settings) -> datetime:
    target_date = _next_weekday(local.date())
    if target_date != local.date():
        return datetime.combine(
            target_date,
            time(settings.outreach_start_hour),
            tzinfo=local.tzinfo,
        )
    if local.hour < settings.outreach_start_hour:
        return datetime.combine(
            target_date,
            time(settings.outreach_start_hour),
            tzinfo=local.tzinfo,
        )
    if local.hour >= settings.outreach_end_hour:
        target_date = _next_weekday(target_date + timedelta(days=1))
        return datetime.combine(
            target_date,
            time(settings.outreach_start_hour),
            tzinfo=local.tzinfo,
        )
    return local


def is_local_business_time(now: datetime, timezone: str, settings: Settings) -> bool:
    local = aware(now).astimezone(timezone_for(timezone))
    return (
        local.weekday() < 5
        and settings.outreach_start_hour <= local.hour < settings.outreach_end_hour
    )


def next_local_window(
    now: datetime,
    timezone: str,
    settings: Settings,
    days: int = 0,
) -> datetime:
    if days < 0:
        raise ValueError("days must be non-negative")
    zone = timezone_for(timezone)
    local = aware(now).astimezone(zone) + timedelta(days=days)
    return _clamp_to_business_window(local, settings).astimezone(UTC)


def business_time_after_delay(
    now: datetime,
    timezone: str,
    settings: Settings,
    *,
    delay_seconds: int,
    days: int = 0,
) -> datetime:
    """Apply a delay inside a weekday window, rolling overflow to the next workday."""
    if delay_seconds < 0 or days < 0:
        raise ValueError("delay and days must be non-negative")
    zone = timezone_for(timezone)
    local = aware(now).astimezone(zone) + timedelta(days=days)
    local = _clamp_to_business_window(local, settings)
    local = _clamp_to_business_window(local + timedelta(seconds=delay_seconds), settings)
    return local.astimezone(UTC)


def humanized_outreach_time(
    now: datetime,
    timezone: str,
    settings: Settings,
    *,
    seed: str,
    days: int = 0,
    minimum_minutes: int | None = None,
    maximum_minutes: int | None = None,
) -> datetime:
    minimum = (
        settings.outreach_jitter_min_minutes if minimum_minutes is None else minimum_minutes
    )
    maximum = (
        settings.outreach_jitter_max_minutes if maximum_minutes is None else maximum_minutes
    )
    delay = stable_int(seed, minimum * 60, maximum * 60)
    return business_time_after_delay(
        now,
        timezone,
        settings,
        delay_seconds=delay,
        days=days,
    )


def conversation_reply_time(
    now: datetime,
    timezone: str,
    settings: Settings,
    *,
    seed: str,
    intent: str,
    body: str,
) -> datetime:
    """Pace replies by intent and message complexity, then enforce local work hours."""
    profiles = {
        "meeting_selection": (35, 90),
        "call_request": (45, 120),
        "interested_with_tier": (75, 180),
        "interested": (90, 240),
        "question": (150, 360),
        "objection": (240, 540),
        "negative": (120, 300),
    }
    low, high = profiles.get(intent, (120, 360))
    complexity = min(180, max(0, len(body.strip()) - 80) + body.count("?") * 30)
    low = max(settings.conversation_reply_min_seconds, low + complexity // 2)
    high = min(settings.conversation_reply_max_seconds, high + complexity)
    high = max(low, high)
    delay = stable_int(seed, low, high)
    return business_time_after_delay(
        now,
        timezone,
        settings,
        delay_seconds=delay,
    )
