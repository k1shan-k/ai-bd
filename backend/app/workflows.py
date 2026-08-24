from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import (
    AdapterRegistry,
    AmbiguousProviderError,
    RetryableProviderError,
    TerminalProviderError,
)
from app.brain import GeneratedOutreach, compose_outreach
from app.config import Settings
from app.llm import LLMClient, llm_client
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
from app.timing import (
    aware,
    business_time_after_delay,
    humanized_outreach_time,
    next_local_window,
    stable_int,
)


def ensure_conversation(session: Session, lead: EventLead) -> Conversation:
    conversation = session.scalar(select(Conversation).where(Conversation.lead_id == lead.id))
    if conversation:
        return conversation
    conversation = Conversation(lead_id=lead.id)
    session.add(conversation)
    session.flush()
    return conversation


async def compose_message(
    client: LLMClient,
    settings: Settings,
    *,
    lead: EventLead,
    contact: Contact,
    context: ContextVersion,
    report: ResearchReport,
    action: str,
    channel: str,
) -> GeneratedOutreach:
    return await compose_outreach(
        client,
        settings,
        lead=lead,
        contact=contact,
        context=context,
        report=report,
        action=action,
        channel=channel,
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


async def start_lead_workflow(
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
        report = await research_lead(session, lead, settings=settings, context=context)
    elif not report:
        report = await research_lead(session, lead, settings=settings, context=context)

    lead = session.scalar(
        select(EventLead)
        .where(EventLead.id == lead.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    campaign = session.get(Campaign, campaign.id, populate_existing=True)
    if not lead or not campaign:
        raise ValueError("lead or campaign disappeared while research was running")
    if lead.sponsor_answer not in {"yes", "maybe"}:
        raise ValueError("lead eligibility changed while research was running")
    if lead.state in {"won", "lost", "unresponsive", "suppressed"}:
        raise ValueError("lead became terminal while research was running")
    if lead.automation_status != "active":
        raise ValueError("lead automation stopped while research was running")
    if campaign.status != "active" or lead.event_id != campaign.event_id:
        raise ValueError("campaign changed while research was running")
    if lead.campaign_id and lead.campaign_id != campaign.id:
        raise ValueError("lead was assigned to another campaign while research was running")
    if lead.context_version_id and lead.context_version_id != campaign.context_version_id:
        raise ValueError("lead context changed while research was running")
    context = session.get(ContextVersion, campaign.context_version_id, populate_existing=True)
    if not context or context.event_id != campaign.event_id:
        raise ValueError("campaign context changed while research was running")

    first_start = lead.campaign_id is None
    lead.campaign_id = campaign.id
    lead.context_version_id = campaign.context_version_id
    if first_start:
        lead.state = "ready"
        lead.delivery_state = "scheduled"
    due = humanized_outreach_time(
        now,
        lead.contact.timezone,
        settings,
        seed=f"lead:{lead.id}:initial_email:0",
    )
    telegram_due = business_time_after_delay(
        due,
        lead.contact.timezone,
        settings,
        delay_seconds=stable_int(
            f"lead:{lead.id}:initial_telegram:0",
            settings.cross_channel_gap_min_minutes * 60,
            settings.cross_channel_gap_max_minutes * 60,
        ),
    )
    actions = [
        _add_action(session, lead, "initial_email", "email", due, 0),
        _add_action(session, lead, "initial_telegram", "telegram", telegram_due, 0),
    ]
    if not first_start and actions[1].status == "sent":
        schedule_followups(session, lead, campaign, aware(now), settings)
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
    settings: Settings,
) -> None:
    for index, day in enumerate(campaign.followup_days, start=1):
        channel = "telegram" if index % 2 == 1 else "email"
        action_type = "followup"
        if day == campaign.whatsapp_fallback_day and lead.contact.whatsapp_normalized:
            channel = "whatsapp"
            action_type = "whatsapp_fallback"
        due_at = humanized_outreach_time(
            anchor,
            lead.contact.timezone,
            settings,
            seed=f"lead:{lead.id}:{action_type}:{index}",
            days=day,
        )
        _add_action(
            session,
            lead,
            action_type,
            channel,
            due_at,
            index,
        )


async def enqueue_due_actions(
    session: Session,
    now: datetime,
    settings: Settings,
    limit: int = 100,
    brain: LLMClient | None = None,
) -> dict[str, int]:
    now = aware(now)
    brain = brain or llm_client(settings)
    result = {
        "queued": 0,
        "cancelled": 0,
        "rescheduled": 0,
        "quota_deferred": 0,
        "llm_review_required": 0,
        "llm_failed": 0,
        "generation_recovered": 0,
        "generation_discarded": 0,
    }
    stale_before = now - timedelta(seconds=settings.llm_generation_stale_seconds)
    generating = session.scalars(
        select(ScheduledAction).where(ScheduledAction.status == "generating")
    ).all()
    for stale_action in generating:
        generation = stale_action.payload.get("generation") or {}
        raw_started = generation.get("started_at")
        try:
            started_at = aware(datetime.fromisoformat(str(raw_started).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            started_at = datetime.min.replace(tzinfo=now.tzinfo)
        if started_at > stale_before:
            continue
        payload = dict(stale_action.payload)
        payload.pop("generation", None)
        stale_action.payload = payload
        stale_action.status = "pending"
        stale_action.attempt += 1
        result["generation_recovered"] += 1
    if result["generation_recovered"]:
        session.commit()

    action_ids = session.scalars(
        select(ScheduledAction.id)
        .where(ScheduledAction.status == "pending", ScheduledAction.due_at <= now)
        .order_by(ScheduledAction.due_at)
        .limit(limit)
    ).all()
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
        generated: GeneratedOutreach | None = None
        if action.payload.get("body"):
            body = str(action.payload["body"])
            subject = action.payload.get("subject")
            generation_provenance = dict(action.payload.get("generation_provenance") or {})
        else:
            claim_token = str(uuid4())
            context_id = context.id
            report_id = report.id
            payload = dict(action.payload)
            payload["generation"] = {
                "claim_token": claim_token,
                "started_at": now.isoformat(),
                "context_version_id": context_id,
                "research_report_id": report_id,
            }
            action.payload = payload
            action.status = "generating"
            session.commit()
            try:
                generated = await compose_message(
                    brain,
                    settings,
                    lead=lead,
                    contact=contact,
                    context=context,
                    report=report,
                    action=action.action_type,
                    channel=action.channel,
                )
            except Exception as exc:
                failed_lead = session.scalar(
                    select(EventLead)
                    .where(EventLead.id == lead.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                failed_action = session.scalar(
                    select(ScheduledAction)
                    .where(ScheduledAction.id == action_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                failed_generation = (
                    failed_action.payload.get("generation") if failed_action else None
                ) or {}
                if (
                    failed_action
                    and failed_action.status == "generating"
                    and failed_generation.get("claim_token") == claim_token
                ):
                    failed_action.status = "failed"
                    failed_action.cancelled_reason = "llm_generation_failed"
                    if failed_lead:
                        failed_lead.state = "escalated"
                        session.add(
                            TimelineEvent(
                                lead_id=failed_lead.id,
                                event_type="escalated",
                                data={
                                    "reason": "llm_generation_failed",
                                    "action_id": failed_action.id,
                                    "error_type": type(exc).__name__,
                                },
                            )
                        )
                    result["llm_failed"] += 1
                    session.commit()
                else:
                    session.rollback()
                    result["generation_discarded"] += 1
                continue

            lead = session.scalar(
                select(EventLead)
                .where(EventLead.id == lead.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            action = session.scalar(
                select(ScheduledAction)
                .where(ScheduledAction.id == action_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            generation = (action.payload.get("generation") if action else None) or {}
            if (
                not lead
                or not action
                or action.status != "generating"
                or generation.get("claim_token") != claim_token
            ):
                session.rollback()
                result["generation_discarded"] += 1
                continue
            contact = session.get(Contact, lead.contact_id, populate_existing=True)
            event = session.get(Event, lead.event_id, populate_existing=True)
            context = session.get(ContextVersion, context_id)
            report = session.get(ResearchReport, report_id)
            if (
                not contact
                or not event
                or not context
                or not report
                or lead.context_version_id != context_id
                or report.lead_id != lead.id
            ):
                action.status = "cancelled"
                action.cancelled_reason = "generation_snapshot_stale"
                result["cancelled"] += 1
                session.commit()
                continue
            decision = evaluate_send(session, lead, event, contact, action, now, settings)
            if not decision.allowed:
                action.status = "cancelled"
                action.cancelled_reason = ",".join(decision.reasons)
                session.add(
                    TimelineEvent(
                        lead_id=lead.id,
                        event_type="generation_discarded",
                        data={
                            "action_id": action.id,
                            "reasons": decision.reasons,
                            "claim_token": claim_token,
                        },
                    )
                )
                result["generation_discarded"] += 1
                session.commit()
                continue
            payload = dict(action.payload)
            payload.pop("generation", None)
            action.payload = payload
            if generated.requires_human_review:
                action.status = "cancelled"
                action.cancelled_reason = "llm_review_required"
                lead.state = "escalated"
                session.add(
                    TimelineEvent(
                        lead_id=lead.id,
                        event_type="escalated",
                        data={
                            "reason": "llm_review_required",
                            "action_id": action.id,
                            "review_reasons": generated.review_reasons,
                            "confidence": generated.confidence,
                            "provider": generated.provider,
                            "model": generated.model,
                            "prompt_hash": generated.prompt_hash,
                        },
                    )
                )
                result["llm_review_required"] += 1
                session.commit()
                continue
            body = generated.body
            subject = generated.subject
            generation_provenance = generated.provenance()
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
                action.status = "pending"
                action.due_at = next_local_window(now, contact.timezone, settings, days=1)
                result["quota_deferred"] += 1
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
                    # Availability must follow the workflow clock so a simulated or replayed
                    # cycle can dispatch what it just queued instead of waiting on wall time.
                    available_at=now,
                    payload={
                        "action_id": action.id,
                        "lead_id": lead.id,
                        "channel": action.channel,
                        "identity": identity,
                        "body": body,
                        "subject": subject,
                        "generation_provenance": generation_provenance,
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
                    "subject": payload.get("subject"),
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
                        **dict(payload.get("generation_provenance") or {}),
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
                    schedule_followups(session, lead, campaign, aware(now), settings)
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
    queued = await enqueue_due_actions(session, now, settings, limit)
    dispatched = await dispatch_outbox(session, adapters, settings, now, limit)
    return {
        "expired_offer_reservations": expired_offers,
        "enqueue": queued,
        "dispatch": dispatched,
    }
