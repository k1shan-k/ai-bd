import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Contact,
    ContextVersion,
    Event,
    EventLead,
    ScheduledAction,
    SuppressionEntry,
    TelegramDailyQuota,
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


def canonical_policy_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def policy_phrase_present(term: str, text: str) -> bool:
    canonical_term = canonical_policy_text(term)
    return bool(canonical_term) and f" {canonical_term} " in f" {text} "


def is_suppressed(session: Session, contact: Contact) -> bool:
    identities = {
        ("email", contact.email_normalized),
        ("telegram", contact.telegram_normalized),
    }
    if contact.whatsapp_normalized:
        identities.add(("whatsapp", contact.whatsapp_normalized))
    entries = session.scalars(select(SuppressionEntry)).all()
    return any((entry.identity_type, entry.identity_value) in identities for entry in entries)


def evaluate_send(
    session: Session,
    lead: EventLead,
    event: Event,
    contact: Contact,
    action: ScheduledAction,
    now: datetime,
    settings: Settings,
) -> PolicyDecision:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    cutoff = event.outreach_cutoff_at
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    checks: dict[str, bool] = {
        "eligible": lead.sponsor_answer in {"yes", "maybe"},
        "active": lead.automation_status == "active" or action.action_type == "manual_reply",
        "not_terminal": lead.state not in {"won", "lost", "unresponsive", "suppressed"},
        "not_replied": lead.last_reply_at is None
        or action.action_type in {"conversation_reply", "manual_reply", "meeting_slots"},
        "not_suppressed": not is_suppressed(session, contact),
        "before_cutoff": cutoff is None or now < cutoff,
        "pending": action.status in {"pending", "queued"},
    }
    try:
        local_now = now.astimezone(ZoneInfo(contact.timezone))
        checks["local_daytime"] = (
            settings.outreach_start_hour <= local_now.hour < settings.outreach_end_hour
        )
    except ZoneInfoNotFoundError:
        checks["local_daytime"] = False
    reasons = [name for name, passed in checks.items() if not passed]
    return PolicyDecision(allowed=not reasons, reasons=reasons, checks=checks)


def reserve_telegram_new_contact(
    session: Session, quota_date: date, limit: int
) -> tuple[bool, int]:
    """Reserve one account-wide new-contact slot in the caller's transaction.

    PostgreSQL serializes contenders with a row lock. The nested insert protects the rare
    first-reservation race without rolling back unrelated workflow changes. SQLite serializes
    writers, which preserves the same invariant in local/test mode.
    """
    quota = session.scalar(
        select(TelegramDailyQuota)
        .where(TelegramDailyQuota.quota_date == quota_date)
        .with_for_update()
    )
    if quota is None:
        try:
            with session.begin_nested():
                session.add(
                    TelegramDailyQuota(
                        quota_date=quota_date,
                        reserved_count=0,
                        limit_count=limit,
                    )
                )
                session.flush()
        except IntegrityError:
            pass
        quota = session.scalar(
            select(TelegramDailyQuota)
            .where(TelegramDailyQuota.quota_date == quota_date)
            .with_for_update()
        )
    if quota is None:
        raise RuntimeError("failed to initialize Telegram quota ledger")
    # Preserve the configured cap that applied when the day started; never raise it mid-day.
    quota.limit_count = min(quota.limit_count, limit)
    if quota.reserved_count >= quota.limit_count:
        return False, quota.reserved_count
    quota.reserved_count += 1
    session.flush()
    return True, quota.reserved_count


def validate_offer(
    context: ContextVersion,
    package_id: str,
    offered_price: Decimal,
    perks: list[str],
    rationale: str = "",
) -> tuple[bool, list[str], dict]:
    compiled = context.compiled
    package = next((p for p in compiled["packages"] if p["id"] == package_id), None)
    if not package:
        return False, ["unknown_package"], {}
    list_price = Decimal(package["list_price"])
    min_price = Decimal(package["min_price"])
    policy = compiled["negotiation"]
    max_discount = Decimal(policy["max_discount_percent"])
    actual_discount = (
        (list_price - offered_price) / list_price * Decimal("100")
        if list_price
        else Decimal("0")
    )
    reasons: list[str] = []
    if offered_price < min_price:
        reasons.append("below_minimum_price")
    if actual_discount > max_discount:
        reasons.append("discount_exceeds_cap")
    allowed = set(package.get("perks", [])) | set(policy.get("allowed_custom_perks", []))
    invalid_perks = sorted(set(perks) - allowed)
    if invalid_perks:
        reasons.append("forbidden_or_unknown_perks")
    proposal_text = canonical_policy_text(" ".join([rationale, *perks]))
    forbidden_promises = [
        str(term)
        for term in policy.get("forbidden_promises", [])
        if policy_phrase_present(str(term), proposal_text)
    ]
    mandatory_escalations = [
        str(term)
        for term in policy.get("mandatory_escalation", [])
        if policy_phrase_present(str(term), proposal_text)
    ]
    if forbidden_promises:
        reasons.append("forbidden_promise")
    if mandatory_escalations:
        reasons.append("mandatory_escalation")
    inventory = int(compiled.get("inventory", {}).get(package_id, 0))
    if inventory <= 0:
        reasons.append("inventory_unavailable")
    details = {
        "package": package,
        "actual_discount_percent": str(actual_discount.quantize(Decimal("0.01"))),
        "invalid_perks": invalid_perks,
        "forbidden_promises": forbidden_promises,
        "mandatory_escalations": mandatory_escalations,
        "inventory_available": inventory,
    }
    return not reasons, reasons, details
