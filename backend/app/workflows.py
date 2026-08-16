from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import (
    AdapterRegistry,
    AmbiguousProviderError,
    RetryableProviderError,
    TerminalProviderError,
)
from app.config import Settings
from app.models import (
    Campaign,
    Contact,
    ContextVersion,
    Conversation,
    Event,
    EventLead,
    Message,
    OutboxEvent,
    ResearchReport,
    ScheduledAction,
    TimelineEvent,
    utcnow,
)
from app.policy import evaluate_send, reserve_telegram_new_contact
from app.research import research_lead


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def next_local_window(now: datetime, timezone: str, settings: Settings, days: int = 0) -> datetime:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local = aware(now).astimezone(zone) + timedelta(days=days)
    target_date = local.date()
    if days == 0 and local.hour >= settings.outreach_end_hour:
        target_date += timedelta(days=1)
    if days == 0 and settings.outreach_start_hour <= local.hour < settings.outreach_end_hour:
        return aware(now)
    target = datetime.combine(target_date, time(settings.outreach_start_hour), tzinfo=zone)
    return target.astimezone(UTC)


def ensure_conversation(session: Session, lead: EventLead) -> Conversation:
    conversation = session.scalar(select(Conversation).where(Conversation.lead_id == lead.id))
    if conversation:
        return conversation
    conversation = Conversation(lead_id=lead.id)
    session.add(conversation)
    session.flush()
    return conversation


def compose_message(
    *, lead: EventLead, contact: Contact, context: ContextVersion, report: ResearchReport, action: str
) -> str:
    event_name = context.compiled.get("event", {}).get("name", "our event")
    company = contact.company_name or "your team"
    fit_angle = (
        report.fit_angles[0]
        if report.fit_angles
        else f"Explore how {company} could connect with the event audience."
    )
    if action == "initial_email":
        return (
            f"Hi {contact.full_name},\n\nThanks for indicating that {company} may be interested "
            f"in sponsoring {event_name}. Our sponsorship team identified this possible fit: "
            f"{fit_angle} Would you like a quick overview of the available packages?\n\n"
            "Best,\nThe Sponsorship Team"
        )
    if action == "initial_telegram":
        return (
            f"Hi {contact.full_name} — the sponsorship team for {event_name} here. "
            "You indicated possible sponsorship interest when registering. Happy to share the "
            "short package overview or answer questions here."
        )
    if action == "whatsapp_fallback":
        return (
            f"Hi {contact.full_name}, this is the {event_name} sponsorship team. "
            "Following up on the sponsorship interest from your registration. Would a short "
            "package summary be useful?"
        )
    return (
        f"Hi {contact.full_name}, a quick follow-up from the {event_name} sponsorship team. "
        "Would you like details on the available sponsorship options, or should we close the loop?"
    )


def _add_action(
    session: Session,
    lead: EventLead,
    action_type: str,
    channel: str,
    due_at: datetime,
    sequence: int,
) -> ScheduledAction:
    key = f"lead:{lead.id}:{action_type}:{sequence}"
    existing = session.scalar(select(ScheduledAction).where(ScheduledAction.idempotency_key == key))
    if existing:
        resumable_reasons = {
            "operator_paused",
            "operator_manual",
            "manual_takeover",
            "lead_won",
            "lead_lost",
            "lead_unresponsive",
        }
        if existing.status == "cancelled" and existing.cancelled_reason in resumable_reasons:
            existing.status = "pending"
            existing.cancelled_reason = None
            existing.due_at = due_at
            existing.attempt = 0
            outbox = session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.idempotency_key == f"send:{existing.id}"
                )
            )
            if outbox and outbox.status == "cancelled":
                outbox.status = "pending"
                outbox.available_at = due_at
                outbox.last_error = None
        return existing
    action = ScheduledAction(
        lead_id=lead.id,
        action_type=action_type,
        channel=channel,
        due_at=due_at,
        idempotency_key=key,
        payload={"sequence": sequence},
    )
    session.add(action)
    return action


def start_lead_workflow(
    session: Session,
    lead: EventLead,
    campaign: Campaign,
    now: datetime,
    settings: Settings,
) -> list[ScheduledAction]:
    session.refresh(lead, with_for_update=True)
    if lead.sponsor_answer not in {"yes", "maybe"}:
        raise ValueError("only yes/maybe leads can start outreach")
    if lead.state in {"won", "lost", "unresponsive", "suppressed"}:
        raise ValueError("terminal leads must be reopened before outreach can start")
    if campaign.status != "active":
        raise ValueError("campaign is not active")
    if lead.event_id != campaign.event_id:
        raise ValueError("lead and campaign belong to different events")
    if lead.campaign_id and lead.campaign_id != campaign.id:
        raise ValueError("lead is already assigned to a different campaign")
    context = session.get(ContextVersion, campaign.context_version_id)
    if not context or context.event_id != campaign.event_id:
        raise ValueError("campaign context does not belong to the campaign event")
    if lead.automation_status != "active":
        raise ValueError("lead automation is not active")
    if lead.context_version_id and lead.context_version_id != campaign.context_version_id:
        raise ValueError("lead is already pinned to a different context version")
    report = session.scalar(
        select(ResearchReport)
        .where(ResearchReport.lead_id == lead.id)
        .order_by(ResearchReport.created_at.desc())
    )
    if settings.provider_mode == "live" and (
        not report or report.provider != settings.research_provider
    ):
        report = research_lead(session, lead, settings=settings)
    elif not report:
        report = research_lead(session, lead, settings=settings)
    first_start = lead.campaign_id is None
    lead.campaign_id = campaign.id
    lead.context_version_id = campaign.context_version_id
    if first_start:
        lead.state = "ready"
        lead.delivery_state = "scheduled"
    due = next_local_window(now, lead.contact.timezone, settings)
    actions = [
        _add_action(session, lead, "initial_email", "email", due, 0),
        _add_action(session, lead, "initial_telegram", "telegram", due + timedelta(minutes=5), 0),
    ]
    if not first_start and actions[1].status == "sent":
        schedule_followups(session, lead, campaign, aware(now))
    if first_start:
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="workflow_started",
                data={"campaign_id": campaign.id, "context_version_id": campaign.context_version_id},
            )
        )
    session.commit()
    return actions


def schedule_followups(
    session: Session,
    lead: EventLead,
    campaign: Campaign,
    anchor: datetime,
) -> None:
    for index, day in enumerate(campaign.followup_days, start=1):
        channel = "telegram" if index % 2 == 1 else "email"
        action_type = "followup"
        if day == campaign.whatsapp_fallback_day and lead.contact.whatsapp_normalized:
            channel = "whatsapp"
            action_type = "whatsapp_fallback"
        _add_action(
            session,
            lead,
            action_type,
            channel,
            aware(anchor) + timedelta(days=day),
            index,
        )


def enqueue_due_actions(
    session: Session,
    now: datetime,
    settings: Settings,
    limit: int = 100,
) -> dict[str, int]:
    now = aware(now)
    action_ids = session.scalars(
        select(ScheduledAction.id)
        .where(ScheduledAction.status == "pending", ScheduledAction.due_at <= now)
        .order_by(ScheduledAction.due_at)
        .limit(limit)
    ).all()
    result = {"queued": 0, "cancelled": 0, "rescheduled": 0, "quota_deferred": 0}
    for action_id in action_ids:
        preview = session.get(ScheduledAction, action_id)
        if not preview:
            continue
        lead = session.scalar(
            select(EventLead)
            .where(EventLead.id == preview.lead_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        action = session.scalar(
            select(ScheduledAction)
            .where(ScheduledAction.id == action_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not action or action.status != "pending":
            session.rollback()
            continue
        if not lead:
            action.status = "cancelled"
            action.cancelled_reason = "lead_missing"
            result["cancelled"] += 1
            session.commit()
            continue
        contact = session.get(Contact, lead.contact_id)
        event = session.get(Event, lead.event_id)
        if not contact or not event:
            action.status = "cancelled"
            action.cancelled_reason = "contact_or_event_missing"
            result["cancelled"] += 1
            session.commit()
            continue
        decision = evaluate_send(session, lead, event, contact, action, now, settings)
        if not decision.allowed:
            if decision.reasons == ["local_daytime"]:
                action.due_at = next_local_window(now, contact.timezone, settings)
                result["rescheduled"] += 1
            else:
                action.status = "cancelled"
                action.cancelled_reason = ",".join(decision.reasons)
                result["cancelled"] += 1
            session.add(
                TimelineEvent(
                    lead_id=lead.id,
                    event_type="policy_decision",
                    data={
                        "action_id": action.id,
                        "allowed": decision.allowed,
                        "checks": decision.checks,
                        "reasons": decision.reasons,
                    },
                )
            )
            session.commit()
            continue

        if settings.provider_mode == "live":
            live_report = session.scalar(
                select(ResearchReport)
                .where(ResearchReport.lead_id == lead.id)
                .order_by(ResearchReport.created_at.desc())
            )
            if not live_report or live_report.provider != settings.research_provider:
                action.status = "cancelled"
                action.cancelled_reason = "live_research_provider_mismatch"
                result["cancelled"] += 1
                session.commit()
                continue

        if action.channel == "telegram" and action.action_type == "initial_telegram":
            try:
                quota_zone = ZoneInfo(settings.telegram_quota_timezone)
            except ZoneInfoNotFoundError:
                quota_zone = ZoneInfo("UTC")
            quota_date = now.astimezone(quota_zone).date()
            reserved, _count = reserve_telegram_new_contact(
                session, quota_date, settings.telegram_daily_new_contact_limit
            )
            if not reserved:
                action.due_at = next_local_window(now, contact.timezone, settings, days=1)
                result["quota_deferred"] += 1
                session.commit()
                continue

        context = session.get(ContextVersion, lead.context_version_id)
        report = session.scalar(
            select(ResearchReport)
            .where(ResearchReport.lead_id == lead.id)
            .order_by(ResearchReport.created_at.desc())
        )
        if not context or not report:
            action.status = "cancelled"
            action.cancelled_reason = "context_or_research_missing"
            result["cancelled"] += 1
            session.commit()
            continue
        if float(report.confidence) < settings.minimum_research_confidence:
            action.status = "cancelled"
            action.cancelled_reason = "research_confidence_below_threshold"
            lead.state = "escalated"
            session.add(
                TimelineEvent(
                    lead_id=lead.id,
                    event_type="escalated",
                    data={
                        "reason": "research_confidence_below_threshold",
                        "confidence": str(report.confidence),
                    },
                )
            )
            result["cancelled"] += 1
            session.commit()
            continue
        body = action.payload.get("body") or compose_message(
            lead=lead, contact=contact, context=context, report=report, action=action.action_type
        )
        identity = {
            "email": contact.email_normalized,
            "telegram": contact.telegram_normalized,
            "whatsapp": contact.whatsapp_normalized,
        }[action.channel]
        if not identity:
            action.status = "cancelled"
            action.cancelled_reason = "channel_identity_missing"
            result["cancelled"] += 1
            session.commit()
            continue
        outbox_key = f"send:{action.id}"
        outbox = session.scalar(select(OutboxEvent).where(OutboxEvent.idempotency_key == outbox_key))
        if not outbox:
            session.add(
                OutboxEvent(
                    aggregate_type="lead",
                    aggregate_id=lead.id,
                    event_type="message.send",
                    idempotency_key=outbox_key,
                    payload={
                        "action_id": action.id,
                        "lead_id": lead.id,
                        "channel": action.channel,
                        "identity": identity,
                        "body": body,
                        "action_type": action.action_type,
                        "contact_name": contact.full_name,
                        "contact_email": contact.email_normalized,
                        "context_version_id": context.id,
                        "research_report_id": report.id,
                    },
                )
            )
        action.status = "queued"
        result["queued"] += 1
        session.commit()
    return result


async def dispatch_outbox(
    session: Session,
    adapters: AdapterRegistry,
    settings: Settings,
    now: datetime,
    limit: int = 100,
) -> dict[str, int]:
    event_ids = session.scalars(
        select(OutboxEvent.id)
        .where(OutboxEvent.status == "pending", OutboxEvent.available_at <= aware(now))
        .order_by(OutboxEvent.created_at)
        .limit(limit)
    ).all()
    result = {"sent": 0, "cancelled": 0, "failed": 0}
    for outbox_id in event_ids:
        preview = session.get(OutboxEvent, outbox_id)
        if not preview:
            continue
        payload = preview.payload
        # All send/cancel paths lock lead -> action -> outbox in this order.
        lead = session.scalar(
            select(EventLead)
            .where(EventLead.id == payload["lead_id"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        action = session.scalar(
            select(ScheduledAction)
            .where(ScheduledAction.id == payload["action_id"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        outbox = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.id == outbox_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not outbox or outbox.status != "pending":
            session.rollback()
            continue
        if not lead or not action:
            outbox.status = "cancelled"
            result["cancelled"] += 1
            session.commit()
            continue
        contact = session.get(Contact, lead.contact_id)
        event = session.get(Event, lead.event_id)
        report = session.get(ResearchReport, payload.get("research_report_id"))
        if not contact or not event:
            outbox.status = "cancelled"
            action.status = "cancelled"
            result["cancelled"] += 1
            session.commit()
            continue
        if settings.provider_mode == "live" and (
            not report or report.provider != settings.research_provider
        ):
            outbox.status = "cancelled"
            action.status = "cancelled"
            action.cancelled_reason = "live_research_provider_mismatch"
            result["cancelled"] += 1
            session.commit()
            continue
        # The lock fence makes an inbound reply that committed first visible here, while a reply
        # that starts later waits until this send has a definitive provider result.
        decision = evaluate_send(session, lead, event, contact, action, aware(now), settings)
        if not decision.allowed:
            outbox.status = "cancelled"
            action.status = "cancelled"
            action.cancelled_reason = ",".join(decision.reasons)
            result["cancelled"] += 1
            session.commit()
            continue
        try:
            adapter = adapters.messaging[payload["channel"]]
            send_result = await adapter.send(
                identity=payload["identity"],
                body=payload["body"],
                idempotency_key=outbox.idempotency_key,
                metadata={
                    "lead_id": lead.id,
                    "action_type": payload.get("action_type", action.action_type),
                    "contact_name": payload.get("contact_name", contact.full_name),
                },
            )
            conversation = ensure_conversation(session, lead)
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction="outbound",
                    channel=payload["channel"],
                    provider=adapter.name,
                    body=payload["body"],
                    provider_message_id=send_result.provider_message_id,
                    idempotency_key=outbox.idempotency_key,
                    context_version_id=payload["context_version_id"],
                    provenance={
                        "research_report_id": payload["research_report_id"],
                        "policy": decision.checks,
                        "composer": "deterministic-v1",
                    },
                )
            )
            action.status = "sent"
            outbox.status = "processed"
            outbox.processed_at = utcnow()
            lead.delivery_state = f"{payload['channel']}_sent"
            session.add(
                TimelineEvent(
                    lead_id=lead.id,
                    event_type="message_sent",
                    data={
                        "channel": payload["channel"],
                        "action_id": action.id,
                        "provider_message_id": send_result.provider_message_id,
                    },
                )
            )
            if action.action_type == "initial_telegram" and lead.campaign_id:
                campaign = session.get(Campaign, lead.campaign_id)
                if campaign:
                    schedule_followups(session, lead, campaign, aware(now))
            result["sent"] += 1
        except RetryableProviderError as exc:
            outbox.attempts += 1
            action.attempt += 1
            outbox.last_error = str(exc)
            max_attempts = 10 if exc.retry_after_seconds is not None else 3
            if outbox.attempts >= max_attempts:
                outbox.status = "failed"
                action.status = "failed"
            else:
                delay = exc.retry_after_seconds or min(300, 2**outbox.attempts * 15)
                outbox.available_at = aware(now) + timedelta(seconds=delay)
            result["failed"] += 1
        except TerminalProviderError as exc:
            outbox.attempts += 1
            action.attempt += 1
            outbox.last_error = str(exc)
            outbox.status = "failed"
            action.status = "failed"
            result["failed"] += 1
        except AmbiguousProviderError as exc:
            outbox.attempts += 1
            action.attempt += 1
            outbox.last_error = str(exc)
            outbox.status = "reconcile_required"
            action.status = "ambiguous"
            result["failed"] += 1
        except Exception as exc:
            # Unknown provider failures may have happened after remote acceptance. Never replay
            # automatically; an operator or verified delivery callback must reconcile them.
            outbox.attempts += 1
            action.attempt += 1
            outbox.last_error = str(exc)
            outbox.status = "reconcile_required"
            action.status = "ambiguous"
            result["failed"] += 1
        session.commit()
    return result


async def run_worker_cycle(
    session: Session,
    adapters: AdapterRegistry,
    settings: Settings,
    now: datetime,
    limit: int = 100,
) -> dict:
    from app.operations import release_expired_offers

    expired_offers = release_expired_offers(session, aware(now))
    session.commit()
    queued = enqueue_due_actions(session, now, settings, limit)
    dispatched = await dispatch_outbox(session, adapters, settings, now, limit)
    return {
        "expired_offer_reservations": expired_offers,
        "enqueue": queued,
        "dispatch": dispatched,
    }
