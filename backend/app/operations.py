from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters import AdapterRegistry, CalendarConflictError
from app.brain import (
    GeneratedConversationReply,
    InterpretedReply,
    compose_conversation_reply,
    interpret_reply,
    update_conversation_memory,
)
from app.importer import normalize_email, normalize_phone, normalize_telegram
from app.llm import LLMClient, llm_client
from app.models import (
    AuditEvent,
    Contact,
    ContextVersion,
    Conversation,
    EventLead,
    Meeting,
    Message,
    Offer,
    OutboxEvent,
    PackageInventory,
    ProviderEvent,
    ScheduledAction,
    SuppressionEntry,
    TimelineEvent,
    utcnow,
)
from app.policy import validate_offer
from app.timing import conversation_reply_time
from app.workflows import ensure_conversation


def audit(
    session: Session,
    action: str,
    resource_type: str,
    resource_id: str,
    actor: str = "system",
    data: dict | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_type="operator" if actor != "system" else "system",
            actor_id=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            data=data or {},
        )
    )


def suppress_contact(
    session: Session, lead: EventLead, reason: str, source: str = "operator"
) -> None:
    contact = session.get(Contact, lead.contact_id)
    if not contact:
        raise ValueError("contact not found")
    session.refresh(contact, with_for_update=True)
    identities = [
        ("email", contact.email_normalized),
        ("telegram", contact.telegram_normalized),
    ]
    if contact.whatsapp_normalized:
        identities.append(("whatsapp", contact.whatsapp_normalized))
    for identity_type, identity_value in identities:
        exists = session.scalar(
            select(SuppressionEntry).where(
                SuppressionEntry.identity_type == identity_type,
                SuppressionEntry.identity_value == identity_value,
                SuppressionEntry.scope == "global",
            )
        )
        if not exists:
            session.add(
                SuppressionEntry(
                    contact_id=contact.id,
                    identity_type=identity_type,
                    identity_value=identity_value,
                    reason=reason,
                    source=source,
                )
            )
    affected_leads = session.scalars(
        select(EventLead)
        .where(EventLead.contact_id == contact.id)
        .order_by(EventLead.id)
        .with_for_update()
    ).all()
    for affected in affected_leads:
        cancel_pending_outreach(session, affected, reason)
        active_offers = session.scalars(
            select(Offer)
            .where(
                Offer.lead_id == affected.id,
                Offer.status.in_(["proposed", "queued", "accepted"]),
            )
            .with_for_update()
        ).all()
        for offer in active_offers:
            _release_offer_reservation(session, offer, "cancelled", reason)
        affected.state = "suppressed"
        affected.automation_status = "stopped"
        session.add(
            TimelineEvent(
                lead_id=affected.id,
                event_type="globally_suppressed",
                actor_type="operator" if source == "operator" else "prospect",
                data={"reason": reason, "scope": "global"},
            )
        )
    audit(
        session,
        "contact.suppress",
        "contact",
        contact.id,
        source,
        {"reason": reason, "affected_leads": len(affected_leads)},
    )


def cancel_pending_outreach(session: Session, lead: EventLead, reason: str) -> int:
    actions = session.scalars(
        select(ScheduledAction)
        .where(
            ScheduledAction.lead_id == lead.id,
            ScheduledAction.status.in_(["pending", "queued", "generating"]),
        )
        .with_for_update()
    ).all()
    for action in actions:
        action.status = "cancelled"
        action.cancelled_reason = reason
    outbox_events = session.scalars(
        select(OutboxEvent)
        .where(
            OutboxEvent.aggregate_id == lead.id,
            OutboxEvent.status == "pending",
        )
        .with_for_update()
    ).all()
    for item in outbox_events:
        item.status = "cancelled"
    return len(actions)


def _find_contact(session: Session, channel: str, identity: str) -> Contact | None:
    if channel == "email":
        return session.scalar(
            select(Contact).where(Contact.email_normalized == normalize_email(identity))
        )
    if channel == "telegram":
        return session.scalar(
            select(Contact).where(Contact.telegram_normalized == normalize_telegram(identity))
        )
    normalized = normalize_phone(identity)
    return session.scalar(
        select(Contact).where(Contact.whatsapp_normalized == normalized)
    ) if normalized else None


async def _classify(
    body: str,
    context: ContextVersion | None,
    adapters: AdapterRegistry,
    recent_messages: list[dict],
    brain: LLMClient | None = None,
) -> InterpretedReply:
    return await interpret_reply(
        brain or llm_client(adapters.settings),
        adapters.settings,
        body=body,
        context=context,
        recent_messages=recent_messages,
    )


async def _draft_conversation_response(
    adapters: AdapterRegistry,
    *,
    contact: Contact,
    context: ContextVersion,
    interpretation: InterpretedReply,
    channel: str,
    inbound_body: str,
    recent_messages: list[dict],
    conversation_summary: str,
    meeting_slots: list[datetime] | None = None,
    brain: LLMClient | None = None,
) -> GeneratedConversationReply:
    return await compose_conversation_reply(
        brain or llm_client(adapters.settings),
        adapters.settings,
        contact=contact,
        context=context,
        interpretation=interpretation,
        channel=channel,
        inbound_body=inbound_body,
        recent_messages=recent_messages,
        authoritative_meeting_slots=[slot.isoformat() for slot in (meeting_slots or [])],
        conversation_summary=conversation_summary,
    )


@dataclass
class ConversationGenerationPhase:
    draft: GeneratedConversationReply | None
    slots: list[datetime]
    lead: EventLead | None
    claimed: ProviderEvent | None
    inbound_message: Message | None
    conversation: Conversation | None
    contact: Contact | None
    context: ContextVersion | None
    stale_reason: str | None = None
    error_type: str | None = None


async def _generate_conversation_phase(
    session: Session,
    adapters: AdapterRegistry,
    *,
    brain: LLMClient | None,
    lead: EventLead,
    claimed: ProviderEvent,
    inbound_message: Message,
    conversation: Conversation,
    contact: Contact,
    context: ContextVersion,
    interpretation: InterpretedReply,
    channel: str,
    body: str,
    now: datetime,
    needs_meeting_slots: bool,
) -> ConversationGenerationPhase:
    reply_claim_token = str(uuid4())
    lead_id = lead.id
    claimed_id = claimed.id
    inbound_message_id = inbound_message.id
    conversation_id = conversation.id
    contact_id = contact.id
    context_id = context.id
    provider_event_id = claimed.provider_event_id
    recent_rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(16)
    ).all()
    recent_messages = [
        {"direction": item.direction, "channel": item.channel, "body": item.body}
        for item in reversed(recent_rows)
    ]
    conversation_summary = conversation.summary
    claim_payload = dict(claimed.payload)
    claim_payload.update(
        {
            "processing_status": "generating_reply",
            "reply_claim_token": reply_claim_token,
            "reply_context_version_id": context_id,
        }
    )
    claimed.payload = claim_payload
    session.commit()

    slots: list[datetime] = []
    try:
        if needs_meeting_slots:
            slots = await adapters.calendar.slots(after=now, timezone=contact.timezone)
        draft = await _draft_conversation_response(
            adapters,
            contact=contact,
            context=context,
            interpretation=interpretation,
            channel=channel,
            inbound_body=body,
            recent_messages=recent_messages,
            conversation_summary=conversation_summary,
            meeting_slots=slots,
            brain=brain,
        )
    except Exception as exc:
        lead = session.scalar(
            select(EventLead)
            .where(EventLead.id == lead_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        claimed = session.get(ProviderEvent, claimed_id, populate_existing=True)
        if lead and claimed:
            current_claim = dict(claimed.payload)
            if current_claim.get("reply_claim_token") == reply_claim_token:
                current_claim.update(
                    {
                        "processing_status": "reply_generation_failed",
                        "error_type": type(exc).__name__,
                    }
                )
                claimed.payload = current_claim
                lead.state = "escalated"
                session.add(
                    TimelineEvent(
                        lead_id=lead.id,
                        event_type="escalated",
                        data={
                            "reason": "conversation_reply_generation_failed",
                            "provider_event_id": provider_event_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                )
                session.commit()
            else:
                session.rollback()
        return ConversationGenerationPhase(
            draft=None,
            slots=slots,
            lead=lead,
            claimed=claimed,
            inbound_message=None,
            conversation=None,
            contact=None,
            context=None,
            error_type=type(exc).__name__,
        )

    lead = session.scalar(
        select(EventLead)
        .where(EventLead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    inbound_message = session.get(Message, inbound_message_id, populate_existing=True)
    claimed = session.get(ProviderEvent, claimed_id, populate_existing=True)
    conversation = session.get(Conversation, conversation_id, populate_existing=True)
    contact = session.get(Contact, contact_id, populate_existing=True)
    context = session.get(ContextVersion, context_id, populate_existing=True)
    latest_inbound_id = session.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    current_claim = dict(claimed.payload) if claimed else {}
    stale_reason = None
    if not lead or not inbound_message or not claimed or not conversation or not contact or not context:
        stale_reason = "processing_snapshot_missing"
    elif current_claim.get("reply_claim_token") != reply_claim_token:
        stale_reason = "reply_claim_replaced"
    elif latest_inbound_id != inbound_message_id:
        stale_reason = "newer_inbound"
    elif lead.state == "suppressed" or lead.automation_status != "active":
        stale_reason = "automation_stopped"
    elif lead.context_version_id != context_id:
        stale_reason = "context_version_changed"
    if stale_reason:
        if claimed:
            current_claim.update(
                {
                    "processing_status": "reply_superseded",
                    "superseded_reason": stale_reason,
                }
            )
            claimed.payload = current_claim
        if lead:
            session.add(
                TimelineEvent(
                    lead_id=lead.id,
                    event_type="conversation_reply_superseded",
                    data={"provider_event_id": provider_event_id, "reason": stale_reason},
                )
            )
        session.commit()
    else:
        current_claim.update({"processing_status": "reply_generated"})
        current_claim.pop("reply_claim_token", None)
        claimed.payload = current_claim
    return ConversationGenerationPhase(
        draft=draft,
        slots=slots,
        lead=lead,
        claimed=claimed,
        inbound_message=inbound_message,
        conversation=conversation,
        contact=contact,
        context=context,
        stale_reason=stale_reason,
    )


def _accept_conversation_draft(
    session: Session,
    lead: EventLead,
    draft: GeneratedConversationReply,
) -> str | None:
    if not draft.requires_human_review:
        return draft.body
    lead.state = "escalated"
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="escalated",
            data={
                "reason": "conversation_reply_review_required",
                "review_reasons": draft.review_reasons,
                "confidence": draft.confidence,
                "provider": draft.provider,
                "model": draft.model,
                "prompt_hash": draft.prompt_hash,
            },
        )
    )
    return None


def _queue_conversation_reply(
    session: Session,
    lead: EventLead,
    channel: str,
    body: str,
    event_key: str,
    queued_at: datetime,
    due_at: datetime,
    generation_provenance: dict | None = None,
) -> None:
    session.add(
        ScheduledAction(
            lead_id=lead.id,
            action_type="conversation_reply",
            channel=channel,
            due_at=due_at,
            idempotency_key=f"lead:{lead.id}:conversation_reply:{event_key}",
            payload={
                "body": body,
                "source_event": event_key,
                "generation_provenance": generation_provenance or {"composer": "deterministic"},
                "timing": {
                    "strategy": "prospect-local-human-v1",
                    "queued_at": queued_at.isoformat(),
                    "due_at": due_at.isoformat(),
                },
            },
        )
    )


def _select_offered_slot(session: Session, lead: EventLead, body: str) -> datetime | None:
    slot_event = session.scalar(
        select(TimelineEvent)
        .where(
            TimelineEvent.lead_id == lead.id,
            TimelineEvent.event_type == "meeting_slots_offered",
        )
        .order_by(TimelineEvent.created_at.desc())
    )
    if not slot_event:
        return None
    raw_slots = slot_event.data.get("slots", [])
    slots = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in raw_slots]
    text = body.lower()
    ordinal = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
    for marker, index in ordinal.items():
        if marker in text and index < len(slots):
            return slots[index]
    for slot in slots:
        if slot.isoformat().lower() in text or slot.strftime("%Y-%m-%d %H:%M").lower() in text:
            return slot
    return None


def _claim_provider_event(
    session: Session,
    provider: str,
    provider_event_id: str,
    event_type: str,
    payload: dict,
) -> ProviderEvent | None:
    event = ProviderEvent(
        provider=provider,
        provider_event_id=provider_event_id,
        event_type=event_type,
        payload=payload,
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
        return event
    except IntegrityError:
        session.expire_all()
        return None


def _escalate_inbound_failure(
    session: Session,
    *,
    lead_id: str,
    claimed_id: str,
    provider_event_id: str,
    reason: str,
    error: Exception,
) -> EventLead | None:
    lead = session.scalar(
        select(EventLead)
        .where(EventLead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    claimed = session.get(ProviderEvent, claimed_id, populate_existing=True)
    if not lead or not claimed:
        session.rollback()
        return lead
    payload = dict(claimed.payload)
    payload.update(
        {
            "processing_status": "failed",
            "failure_reason": reason,
            "error_type": type(error).__name__,
        }
    )
    claimed.payload = payload
    if lead.state != "suppressed":
        lead.state = "escalated"
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="escalated",
            data={
                "reason": reason,
                "provider_event_id": provider_event_id,
                "error_type": type(error).__name__,
            },
        )
    )
    session.commit()
    return lead


async def handle_inbound_event(
    session: Session,
    adapters: AdapterRegistry,
    *,
    provider: str,
    provider_event_id: str,
    channel: str,
    identity: str,
    body: str,
    lead_id: str | None = None,
    occurred_at: datetime | None = None,
    brain: LLMClient | None = None,
) -> dict:
    claimed = _claim_provider_event(
        session,
        provider,
        provider_event_id,
        "message.received",
        {"channel": channel, "identity": identity, "lead_id": lead_id, "body": body},
    )
    if not claimed:
        return {"duplicate": True, "provider_event_id": provider_event_id}
    contact = _find_contact(session, channel, identity)
    if not contact:
        raise ValueError("no contact matches inbound identity")
    if lead_id:
        lead = session.get(EventLead, lead_id)
        if not lead or lead.contact_id != contact.id:
            raise ValueError("lead correlation does not match inbound identity")
        session.refresh(lead, with_for_update=True)
    else:
        candidates = session.scalars(
            select(EventLead)
            .where(
                EventLead.contact_id == contact.id,
                EventLead.state.notin_(["won", "lost", "unresponsive", "suppressed"]),
            )
            .order_by(EventLead.created_at.desc())
            .with_for_update()
        ).all()
        if len(candidates) > 1:
            raise ValueError("inbound identity matches multiple active event leads; lead_id required")
        lead = candidates[0] if candidates else session.scalar(
            select(EventLead)
            .where(EventLead.contact_id == contact.id)
            .order_by(EventLead.created_at.desc())
            .with_for_update()
        )
    if not lead:
        raise ValueError("contact has no event lead")
    now = occurred_at or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    claimed.payload = {
        "channel": channel,
        "identity": identity,
        "lead_id": lead.id,
        "body": body,
    }
    conversation = ensure_conversation(session, lead)
    conversation.preferred_channel = channel
    recent_rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(12)
    ).all()
    recent_messages = [
        {"direction": item.direction, "channel": item.channel, "body": item.body}
        for item in reversed(recent_rows)
    ]
    inbound_message = Message(
        conversation_id=conversation.id,
        direction="inbound",
        channel=channel,
        provider=provider,
        body=body,
        provider_message_id=provider_event_id,
        provenance={"provider": provider},
    )
    session.add(inbound_message)
    coalesced_replies = len(
        session.scalars(
            select(ScheduledAction)
            .where(
                ScheduledAction.lead_id == lead.id,
                ScheduledAction.action_type == "conversation_reply",
                ScheduledAction.status.in_(["pending", "queued", "generating"]),
            )
            .with_for_update()
        ).all()
    )
    cancelled = cancel_pending_outreach(session, lead, "inbound_reply")
    previous_state = lead.state
    previous_reply_at = lead.last_reply_at
    if previous_reply_at is not None and previous_reply_at.tzinfo is None:
        previous_reply_at = previous_reply_at.replace(tzinfo=UTC)
    if previous_reply_at is None or previous_reply_at < now:
        lead.last_reply_at = now
    if previous_state not in {"qualified", "call_booked"}:
        lead.state = "engaged"
    context = session.get(ContextVersion, lead.context_version_id) if lead.context_version_id else None
    selected_slot = _select_offered_slot(session, lead, body) if previous_state == "qualified" else None
    session.flush()
    inbound_message_id = inbound_message.id
    conversation_id = conversation.id
    contact_id = contact.id
    claimed_id = claimed.id
    claim_payload = dict(claimed.payload)
    claim_payload.update(
        {
            "channel": channel,
            "identity": identity,
            "lead_id": lead.id,
            "body": body,
            "message_id": inbound_message_id,
            "processing_status": "interpreting",
        }
    )
    claimed.payload = claim_payload
    session.commit()

    interpretation: InterpretedReply | None = None
    if selected_slot:
        classified_intent, intent, tier = "meeting_selection", "meeting_selection", None
        interpretation_provenance = {
            "interpreter": "deterministic",
            "intent": "meeting_selection",
            "confidence": 1.0,
            "reason": "Matched a currently offered slot.",
        }
    else:
        try:
            interpretation = await _classify(body, context, adapters, recent_messages, brain)
        except Exception as exc:
            lead = session.scalar(
                select(EventLead)
                .where(EventLead.id == lead.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            inbound_message = session.get(Message, inbound_message_id, populate_existing=True)
            claimed = session.get(ProviderEvent, claimed_id, populate_existing=True)
            if not lead or not inbound_message or not claimed:
                session.rollback()
                raise ValueError("inbound interpretation failure snapshot is missing") from exc
            failure = {
                "interpreter": "llm",
                "processing_status": "interpretation_failed",
                "error_type": type(exc).__name__,
                "requires_human_review": True,
            }
            inbound_message.provenance = {"provider": provider, "interpretation": failure}
            claim_payload = dict(claimed.payload)
            claim_payload.update(failure)
            claimed.payload = claim_payload
            if lead.state != "suppressed":
                lead.state = "escalated"
            session.add(
                TimelineEvent(
                    lead_id=lead.id,
                    event_type="escalated",
                    data={
                        "reason": "reply_interpretation_failed",
                        "provider_event_id": provider_event_id,
                        "error_type": type(exc).__name__,
                    },
                )
            )
            session.commit()
            return {
                "duplicate": False,
                "lead_id": lead.id,
                "intent": "uncertain",
                "classified_intent": "uncertain",
                "qualified": lead.state in {"qualified", "call_booked"},
                "call_booked": lead.state == "call_booked",
                "suppressed": lead.state == "suppressed",
                "cancelled_actions": cancelled,
                "coalesced_replies": coalesced_replies,
                "reply_queued": False,
                "interpretation_failed": True,
            }
        classified_intent = interpretation.primary_intent
        tier = interpretation.package_id
        intent = classified_intent
        if interpretation.requires_human_review and intent != "opt_out":
            intent = "uncertain"
        interpretation_provenance = interpretation.provenance()
    lead = session.scalar(
        select(EventLead)
        .where(EventLead.id == lead.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    inbound_message = session.get(Message, inbound_message_id, populate_existing=True)
    claimed = session.get(ProviderEvent, claimed_id, populate_existing=True)
    conversation = session.get(Conversation, conversation_id, populate_existing=True)
    contact = session.get(Contact, contact_id, populate_existing=True)
    if not lead or not inbound_message or not claimed or not conversation or not contact:
        session.rollback()
        raise ValueError("inbound processing snapshot no longer exists")
    context = (
        session.get(ContextVersion, lead.context_version_id, populate_existing=True)
        if lead.context_version_id
        else None
    )
    latest_inbound_id = session.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conversation.id,
            Message.direction == "inbound",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    inbound_message.provenance = {
        "provider": provider,
        "interpretation": interpretation_provenance,
    }
    claim_payload = dict(claimed.payload)
    claim_payload.update(
        {
            "processing_status": "interpreted",
            "classified_intent": classified_intent,
            "interpretation": interpretation_provenance,
        }
    )
    claimed.payload = claim_payload
    superseded_reason = None
    if latest_inbound_id != inbound_message_id:
        superseded_reason = "newer_inbound"
    elif lead.state == "suppressed" or lead.automation_status != "active":
        superseded_reason = "automation_stopped"
    if superseded_reason:
        inbound_message.provenance = {
            **inbound_message.provenance,
            "processing_status": "superseded",
            "superseded_reason": superseded_reason,
        }
        claim_payload = dict(claimed.payload)
        claim_payload.update(
            {
                "processing_status": "superseded",
                "superseded_reason": superseded_reason,
            }
        )
        claimed.payload = claim_payload
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="inbound_processing_superseded",
                data={
                    "provider_event_id": provider_event_id,
                    "reason": superseded_reason,
                    "classified_intent": classified_intent,
                },
            )
        )
        session.commit()
        return {
            "duplicate": False,
            "lead_id": lead.id,
            "intent": "superseded",
            "classified_intent": classified_intent,
            "qualified": lead.state in {"qualified", "call_booked"},
            "call_booked": lead.state == "call_booked",
            "suppressed": lead.state == "suppressed",
            "cancelled_actions": cancelled,
            "coalesced_replies": coalesced_replies,
            "reply_queued": False,
            "superseded": True,
        }

    qualification = context.compiled.get("qualification", {}) if context else {}
    qualification_allowed = (
        intent == "call_request"
        and qualification.get("explicit_call_request_qualifies", False) is True
    ) or (
        intent == "interested_with_tier"
        and qualification.get("interest_plus_tier_qualifies", False) is True
    )
    response: str | None = None
    response_provenance: dict = {"composer": "deterministic"}
    draft: GeneratedConversationReply | None = None

    if intent == "opt_out":
        suppress_contact(session, lead, "prospect_opt_out", source="prospect")
    elif intent == "meeting_selection" and selected_slot:
        try:
            meeting = await book_meeting(
                session,
                adapters,
                lead,
                selected_slot,
                contact.timezone,
            )
            latest_inbound_id = session.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
            if latest_inbound_id != inbound_message_id:
                session.add(
                    TimelineEvent(
                        lead_id=lead.id,
                        event_type="booking_confirmation_superseded",
                        data={
                            "provider_event_id": provider_event_id,
                            "reason": "newer_inbound",
                            "meeting_id": meeting.id,
                        },
                    )
                )
                session.commit()
                return {
                    "duplicate": False,
                    "lead_id": lead.id,
                    "intent": "superseded",
                    "classified_intent": classified_intent,
                    "qualified": lead.state in {"qualified", "call_booked"},
                    "call_booked": lead.state == "call_booked",
                    "suppressed": lead.state == "suppressed",
                    "cancelled_actions": cancelled,
                    "coalesced_replies": coalesced_replies,
                    "reply_queued": False,
                    "superseded": True,
                }
            response = (
                f"You're booked for {meeting.starts_at.isoformat()}. "
                f"Confirmation: {meeting.booking_url}"
            )
        except CalendarConflictError:
            try:
                slots = await adapters.calendar.slots(after=now, timezone=contact.timezone)
            except Exception as exc:
                failed_lead = _escalate_inbound_failure(
                    session,
                    lead_id=lead.id,
                    claimed_id=claimed_id,
                    provider_event_id=provider_event_id,
                    reason="calendar_conflict_recovery_failed",
                    error=exc,
                )
                return {
                    "duplicate": False,
                    "lead_id": failed_lead.id if failed_lead else lead_id,
                    "intent": "uncertain",
                    "classified_intent": classified_intent,
                    "qualified": bool(
                        failed_lead and failed_lead.state in {"qualified", "call_booked"}
                    ),
                    "call_booked": bool(failed_lead and failed_lead.state == "call_booked"),
                    "suppressed": bool(failed_lead and failed_lead.state == "suppressed"),
                    "cancelled_actions": cancelled,
                    "coalesced_replies": coalesced_replies,
                    "reply_queued": False,
                    "calendar_failed": True,
                }
            lead = session.scalar(
                select(EventLead)
                .where(EventLead.id == lead.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            latest_inbound_id = session.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
            conflict_stale_reason = None
            if latest_inbound_id != inbound_message_id:
                conflict_stale_reason = "newer_inbound"
            elif lead.state == "suppressed" or lead.automation_status != "active":
                conflict_stale_reason = "automation_stopped"
            if conflict_stale_reason:
                session.add(
                    TimelineEvent(
                        lead_id=lead.id,
                        event_type="calendar_conflict_reply_superseded",
                        data={
                            "provider_event_id": provider_event_id,
                            "reason": conflict_stale_reason,
                        },
                    )
                )
                session.commit()
                return {
                    "duplicate": False,
                    "lead_id": lead.id,
                    "intent": "superseded",
                    "classified_intent": classified_intent,
                    "qualified": lead.state in {"qualified", "call_booked"},
                    "call_booked": lead.state == "call_booked",
                    "suppressed": lead.state == "suppressed",
                    "cancelled_actions": cancelled,
                    "coalesced_replies": coalesced_replies,
                    "reply_queued": False,
                    "superseded": True,
                }
            lead.state = "qualified"
            session.add(
                TimelineEvent(
                    lead_id=lead.id,
                    event_type="meeting_slot_conflict",
                    data={"requested": selected_slot.isoformat()},
                )
            )
            session.add(
                TimelineEvent(
                    lead_id=lead.id,
                    event_type="meeting_slots_offered",
                    data={
                        "slots": [slot.isoformat() for slot in slots],
                        "timezone": contact.timezone,
                    },
                )
            )
            response = (
                "That time was just taken. The next available options are: "
                + ", ".join(slot.isoformat() for slot in slots)
                + "."
            )
        except Exception as exc:
            failed_lead = _escalate_inbound_failure(
                session,
                lead_id=lead.id,
                claimed_id=claimed_id,
                provider_event_id=provider_event_id,
                reason="calendar_booking_failed",
                error=exc,
            )
            return {
                "duplicate": False,
                "lead_id": failed_lead.id if failed_lead else lead_id,
                "intent": "uncertain",
                "classified_intent": classified_intent,
                "qualified": bool(
                    failed_lead and failed_lead.state in {"qualified", "call_booked"}
                ),
                "call_booked": bool(failed_lead and failed_lead.state == "call_booked"),
                "suppressed": bool(failed_lead and failed_lead.state == "suppressed"),
                "cancelled_actions": cancelled,
                "coalesced_replies": coalesced_replies,
                "reply_queued": False,
                "calendar_failed": True,
            }
    elif intent in {"call_request", "interested_with_tier"} and qualification_allowed:
        lead.state = "qualified"
        lead.qualified_at = now
        phase = await _generate_conversation_phase(
            session,
            adapters,
            brain=brain,
            lead=lead,
            claimed=claimed,
            inbound_message=inbound_message,
            conversation=conversation,
            contact=contact,
            context=context,
            interpretation=interpretation,
            channel=channel,
            body=body,
            now=now,
            needs_meeting_slots=True,
        )
        lead = phase.lead
        claimed = phase.claimed
        inbound_message = phase.inbound_message
        conversation = phase.conversation
        contact = phase.contact
        context = phase.context
        if phase.error_type:
            return {
                "duplicate": False,
                "lead_id": lead.id if lead else lead_id,
                "intent": "uncertain",
                "classified_intent": classified_intent,
                "qualified": bool(lead and lead.state in {"qualified", "call_booked"}),
                "call_booked": bool(lead and lead.state == "call_booked"),
                "suppressed": bool(lead and lead.state == "suppressed"),
                "cancelled_actions": cancelled,
                "coalesced_replies": coalesced_replies,
                "reply_queued": False,
                "generation_failed": True,
            }
        if phase.stale_reason:
            return {
                "duplicate": False,
                "lead_id": lead.id if lead else lead_id,
                "intent": "superseded",
                "classified_intent": classified_intent,
                "qualified": bool(lead and lead.state in {"qualified", "call_booked"}),
                "call_booked": bool(lead and lead.state == "call_booked"),
                "suppressed": bool(lead and lead.state == "suppressed"),
                "cancelled_actions": cancelled,
                "coalesced_replies": coalesced_replies,
                "reply_queued": False,
                "superseded": True,
            }
        slots = phase.slots
        draft = phase.draft
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="meeting_slots_offered",
                data={"slots": [slot.isoformat() for slot in slots], "timezone": contact.timezone},
            )
        )
        response = _accept_conversation_draft(session, lead, draft)
        response_provenance = draft.provenance()
    elif intent in {"call_request", "interested_with_tier"}:
        lead.state = "escalated"
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="escalated",
                data={"reason": "qualification_policy_requires_human", "intent": intent},
            )
        )
        response = (
            "Thanks — the sponsorship team has your request and will review the best next step "
            "before confirming a call."
        )
    elif intent in {"interested", "question", "objection", "negative"} and context and interpretation:
        phase = await _generate_conversation_phase(
            session,
            adapters,
            brain=brain,
            lead=lead,
            claimed=claimed,
            inbound_message=inbound_message,
            conversation=conversation,
            contact=contact,
            context=context,
            interpretation=interpretation,
            channel=channel,
            body=body,
            now=now,
            needs_meeting_slots=False,
        )
        lead = phase.lead
        claimed = phase.claimed
        inbound_message = phase.inbound_message
        conversation = phase.conversation
        contact = phase.contact
        context = phase.context
        if phase.error_type:
            return {
                "duplicate": False,
                "lead_id": lead.id if lead else lead_id,
                "intent": "uncertain",
                "classified_intent": classified_intent,
                "qualified": bool(lead and lead.state in {"qualified", "call_booked"}),
                "call_booked": bool(lead and lead.state == "call_booked"),
                "suppressed": bool(lead and lead.state == "suppressed"),
                "cancelled_actions": cancelled,
                "coalesced_replies": coalesced_replies,
                "reply_queued": False,
                "generation_failed": True,
            }
        if phase.stale_reason:
            return {
                "duplicate": False,
                "lead_id": lead.id if lead else lead_id,
                "intent": "superseded",
                "classified_intent": classified_intent,
                "qualified": bool(lead and lead.state in {"qualified", "call_booked"}),
                "call_booked": bool(lead and lead.state == "call_booked"),
                "suppressed": bool(lead and lead.state == "suppressed"),
                "cancelled_actions": cancelled,
                "coalesced_replies": coalesced_replies,
                "reply_queued": False,
                "superseded": True,
            }
        draft = phase.draft
        response = _accept_conversation_draft(session, lead, draft)
        response_provenance = draft.provenance()
    else:
        lead.state = "escalated"
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="escalated",
                data={
                    "reason": "low_confidence_inbound",
                    "body": body,
                    "classified_intent": classified_intent,
                    "interpretation": interpretation_provenance,
                },
            )
        )

    memory = update_conversation_memory(
        previous_summary=conversation.summary,
        primary_intent=classified_intent,
        secondary_intents=interpretation.secondary_intents if interpretation else [],
        package_id=tier,
        question_topics=interpretation.question_topics if interpretation else [],
        objections=interpretation.objections if interpretation else [],
        confidence=float(interpretation_provenance.get("confidence") or 0.0),
        channel=channel,
        lead_state=lead.state,
        reply=draft if response else None,
    )
    conversation.summary = memory.serialized
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="conversation_memory_updated",
            data=memory.provenance(),
        )
    )

    if response and lead.state != "suppressed":
        reply_due_at = conversation_reply_time(
            now,
            contact.timezone,
            adapters.settings,
            seed=f"lead:{lead.id}:conversation_reply:{provider_event_id}",
            intent=classified_intent,
            body=body,
        )
        _queue_conversation_reply(
            session,
            lead,
            channel,
            response,
            provider_event_id,
            now,
            reply_due_at,
            response_provenance,
        )
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="message_received",
            actor_type="prospect",
            data={
                "channel": channel,
                "provider_event_id": provider_event_id,
                "intent": intent,
                "classified_intent": classified_intent,
                "tier": tier,
                "interpretation": interpretation_provenance,
                "cancelled_actions": cancelled,
                "coalesced_replies": coalesced_replies,
            },
        )
    )
    session.commit()
    return {
        "duplicate": False,
        "lead_id": lead.id,
        "intent": intent,
        "classified_intent": classified_intent,
        "qualified": lead.state in {"qualified", "call_booked"},
        "call_booked": lead.state == "call_booked",
        "suppressed": lead.state == "suppressed",
        "cancelled_actions": cancelled,
        "coalesced_replies": coalesced_replies,
        "reply_queued": response is not None and lead.state != "suppressed",
    }


def handle_delivery_event(
    session: Session,
    *,
    provider: str,
    provider_event_id: str,
    provider_message_id: str,
    status: str,
    occurred_at: datetime | None = None,
    details: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    event_time = occurred_at or utcnow()
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=UTC)
    event_payload = {
        "provider_message_id": provider_message_id,
        "status": status,
        "details": details or {},
        "occurred_at": event_time.isoformat(),
    }
    message = session.scalar(
        select(Message).where(
            Message.provider == provider,
            Message.provider_message_id == provider_message_id,
        )
    )
    if not message and idempotency_key:
        outbox = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.idempotency_key == idempotency_key)
            .with_for_update()
        )
        if outbox:
            payload = outbox.payload
            lead = session.get(EventLead, payload.get("lead_id"))
            action = session.get(ScheduledAction, payload.get("action_id"))
            if lead and action:
                conversation = ensure_conversation(session, lead)
                message = Message(
                    conversation_id=conversation.id,
                    direction="outbound",
                    channel=str(payload.get("channel") or "email"),
                    provider=provider,
                    body=str(payload.get("body") or ""),
                    provider_message_id=provider_message_id,
                    idempotency_key=idempotency_key,
                    context_version_id=payload.get("context_version_id"),
                    provenance={
                        "research_report_id": payload.get("research_report_id"),
                        "reconciled_from_provider_event": provider_event_id,
                    },
                )
                session.add(message)
                action.status = "sent"
                outbox.status = "processed"
                outbox.processed_at = event_time
                lead.delivery_state = f"{payload.get('channel', 'email')}_sent"
                session.flush()
    if not message:
        raise ValueError("outbound provider message is not known")
    conversation = session.get(Conversation, message.conversation_id)
    if not conversation:
        raise ValueError("message conversation is not known")
    if not _claim_provider_event(
        session,
        provider,
        provider_event_id,
        f"message.{status}",
        event_payload,
    ):
        return {"duplicate": True, "provider_event_id": provider_event_id}

    ranks = {
        "accepted": 10,
        "delayed": 15,
        "delivered": 20,
        "read": 30,
        "failed": 40,
        "rejected": 50,
        "bounced": 50,
        "complained": 60,
    }
    previous = str(message.provenance.get("delivery_status") or "")
    previous_time = None
    if message.provenance.get("delivery_occurred_at"):
        try:
            previous_time = datetime.fromisoformat(
                str(message.provenance["delivery_occurred_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            previous_time = None
    regressive = bool(previous_time and event_time < previous_time)
    if not regressive and previous:
        regressive = ranks.get(status, 0) < ranks.get(previous, 0)
    history = list(message.provenance.get("delivery_history") or [])
    history.append({"status": status, "occurred_at": event_time.isoformat(), "details": details or {}})
    message.provenance = {**message.provenance, "delivery_history": history[-20:]}
    if not regressive:
        message.provenance = {
            **message.provenance,
            "delivery_status": status,
            "delivery_details": details or {},
            "delivery_occurred_at": event_time.isoformat(),
        }
        if provider == "ses" and (
            status == "complained"
            or (status == "bounced" and str((details or {}).get("diagnostic", "")).casefold() == "permanent")
        ):
            lead = session.get(EventLead, conversation.lead_id)
            if lead:
                suppress_contact(session, lead, f"ses_{status}", source="provider")
        session.add(
            TimelineEvent(
                lead_id=conversation.lead_id,
                event_type=f"message_{status}",
                data={
                    "provider": provider,
                    "provider_message_id": provider_message_id,
                    "details": details or {},
                    "occurred_at": event_time.isoformat(),
                },
            )
        )
    session.commit()
    return {
        "duplicate": False,
        "ignored_regression": regressive,
        "lead_id": conversation.lead_id,
        "provider_message_id": provider_message_id,
        "status": previous if regressive else status,
    }


def handle_calendar_event(
    session: Session,
    *,
    provider: str,
    provider_event_id: str,
    provider_booking_id: str,
    status: str,
    starts_at: datetime | None = None,
    details: dict | None = None,
    lead_id: str | None = None,
    timezone: str = "UTC",
    booking_url: str | None = None,
) -> dict:
    details = details or {}
    payload = {
        "provider_booking_id": provider_booking_id,
        "status": status,
        "starts_at": starts_at.isoformat() if starts_at else None,
        "details": details,
    }
    meeting = session.scalar(
        select(Meeting)
        .where(
            Meeting.provider == provider,
            Meeting.provider_booking_id == provider_booking_id,
        )
        .with_for_update()
    )
    previous_booking_id = details.get("previous_booking_id")
    previous_meeting = None
    if previous_booking_id and str(previous_booking_id) != provider_booking_id:
        previous_meeting = session.scalar(
            select(Meeting)
            .where(
                Meeting.provider == provider,
                Meeting.provider_booking_id == str(previous_booking_id),
            )
            .with_for_update()
        )
    if not meeting and previous_meeting and starts_at:
        meeting = Meeting(
            lead_id=previous_meeting.lead_id,
            provider=provider,
            provider_booking_id=provider_booking_id,
            starts_at=starts_at,
            timezone=timezone or previous_meeting.timezone,
            status=status,
            booking_url=booking_url,
        )
        session.add(meeting)
        session.flush()
    if not meeting and lead_id and starts_at:
        lead = session.get(EventLead, lead_id)
        if not lead:
            raise ValueError("calendar webhook lead is not known")
        meeting = Meeting(
            lead_id=lead.id,
            provider=provider,
            provider_booking_id=provider_booking_id,
            starts_at=starts_at,
            timezone=timezone,
            status=status,
            booking_url=booking_url,
        )
        session.add(meeting)
        session.flush()
    if not meeting:
        raise ValueError("calendar booking is not known")
    if not _claim_provider_event(
        session, provider, provider_event_id, f"meeting.{status}", payload
    ):
        return {"duplicate": True, "provider_event_id": provider_event_id}
    if previous_meeting and previous_meeting.id != meeting.id and status == "rescheduled":
        previous_meeting.status = "superseded"
    meeting.status = status
    if starts_at:
        meeting.starts_at = starts_at
    if booking_url:
        meeting.booking_url = booking_url
    lead = session.get(EventLead, meeting.lead_id)
    if lead:
        active_meeting = session.scalar(
            select(Meeting.id).where(
                Meeting.lead_id == lead.id,
                Meeting.status.in_(["booked", "rescheduled"]),
            )
        )
        if active_meeting:
            lead.state = "call_booked"
        elif status in {"cancelled", "rejected"} and lead.state == "call_booked":
            lead.state = "qualified"
    session.add(
        TimelineEvent(
            lead_id=meeting.lead_id,
            event_type=f"meeting_{status}",
            data=payload,
        )
    )
    session.commit()
    return {
        "duplicate": False,
        "lead_id": meeting.lead_id,
        "meeting_id": meeting.id,
        "status": status,
    }


def _offer_is_expired(offer: Offer, now: datetime | None = None) -> bool:
    if offer.expires_at is None:
        return False
    expires_at = offer.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (now or utcnow())


def _release_offer_reservation(
    session: Session, offer: Offer, status: str, reason: str
) -> None:
    if offer.status not in {"proposed", "queued", "accepted"}:
        return
    context = session.get(ContextVersion, offer.context_version_id)
    if context:
        lead = session.get(EventLead, offer.lead_id)
        if lead:
            inventory = session.scalar(
                select(PackageInventory)
                .where(
                    PackageInventory.event_id == lead.event_id,
                    PackageInventory.package_id == offer.package_id,
                )
                .with_for_update()
            )
            if inventory and inventory.reserved_count > 0:
                inventory.reserved_count -= 1
    action = session.scalar(
        select(ScheduledAction).where(
            ScheduledAction.idempotency_key == f"offer:{offer.id}:send"
        )
    )
    if action and action.status in {"pending", "queued"}:
        action.status = "cancelled"
        action.cancelled_reason = reason
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == f"send:{action.id}")
        )
        if outbox and outbox.status == "pending":
            outbox.status = "cancelled"
    offer.status = status


def settle_terminal_offers(
    session: Session,
    lead: EventLead,
    state: str,
    accepted_offer_id: str | None = None,
) -> int:
    terminal_states = {"won", "lost", "unresponsive"}
    if state not in terminal_states and lead.state != "won":
        return 0
    offers = session.scalars(
        select(Offer)
        .where(
            Offer.lead_id == lead.id,
            Offer.status.in_(["proposed", "queued", "accepted"]),
        )
        .with_for_update()
    ).all()
    active_offers: list[Offer] = []
    now = utcnow()
    for offer in offers:
        if offer.status in {"proposed", "queued"} and _offer_is_expired(offer, now):
            _release_offer_reservation(session, offer, "expired", "offer_expired")
        else:
            active_offers.append(offer)
    offers = active_offers
    if state == "won":
        selected = next((offer for offer in offers if offer.id == accepted_offer_id), None)
        if not accepted_offer_id or not selected:
            session.commit()
            raise ValueError("won state requires an active accepted_offer_id for this lead")
        conflicting = [
            offer for offer in offers if offer.status == "accepted" and offer.id != selected.id
        ]
        if conflicting:
            raise ValueError("lead already has a different accepted offer; reopen it first")
    for offer in offers:
        if state == "won" and offer.id == accepted_offer_id:
            action = session.scalar(
                select(ScheduledAction).where(
                    ScheduledAction.idempotency_key == f"offer:{offer.id}:send"
                )
            )
            if action and action.status in {"pending", "queued"}:
                action.status = "cancelled"
                action.cancelled_reason = "offer_accepted"
                outbox = session.scalar(
                    select(OutboxEvent).where(
                        OutboxEvent.idempotency_key == f"send:{action.id}"
                    )
                )
                if outbox and outbox.status == "pending":
                    outbox.status = "cancelled"
            offer.status = "accepted"
        else:
            terminal_status = "declined" if state == "won" else state
            if state not in terminal_states:
                terminal_status = "reopened"
            _release_offer_reservation(
                session, offer, terminal_status, f"lead_{state}"
            )
    session.flush()
    return len(offers)


def release_expired_offers(session: Session, now: datetime) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    offers = session.scalars(
        select(Offer)
        .where(
            Offer.status.in_(["proposed", "queued"]),
            Offer.expires_at.is_not(None),
            Offer.expires_at <= now,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for offer in offers:
        _release_offer_reservation(session, offer, "expired", "offer_expired")
    session.flush()
    return len(offers)


def create_offer(
    session: Session,
    lead: EventLead,
    package_id: str,
    offered_price: Decimal,
    perks: list[str],
    rationale: str,
) -> Offer:
    if not lead.context_version_id:
        raise ValueError("lead is not pinned to a context version")
    session.refresh(lead, with_for_update=True)
    if lead.state in {"won", "lost", "unresponsive", "suppressed"}:
        raise ValueError("terminal leads must be reopened before creating an offer")
    context = session.get(ContextVersion, lead.context_version_id)
    if not context:
        raise ValueError("context version not found")
    allowed, reasons, details = validate_offer(
        context, package_id, offered_price, perks, rationale
    )
    if not allowed:
        lead.state = "escalated"
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="offer_rejected_by_policy",
                data={"reasons": reasons, "details": details},
            )
        )
        session.commit()
        raise ValueError(f"offer requires escalation: {', '.join(reasons)}")
    active_offers = session.scalars(
        select(Offer)
        .where(
            Offer.lead_id == lead.id,
            Offer.package_id == package_id,
            Offer.status.in_(["proposed", "queued"]),
        )
        .with_for_update()
    ).all()
    now = utcnow()
    for active in active_offers:
        if _offer_is_expired(active, now):
            _release_offer_reservation(session, active, "expired", "offer_expired")
            continue
        same_offer = (
            active.context_version_id == context.id
            and active.offered_price == offered_price
            and sorted(active.perks) == sorted(perks)
        )
        if same_offer:
            return active
        _release_offer_reservation(session, active, "replaced", "offer_replaced")

    inventory = session.scalar(
        select(PackageInventory)
        .where(
            PackageInventory.event_id == lead.event_id,
            PackageInventory.package_id == package_id,
        )
        .with_for_update()
    )
    if not inventory:
        raise ValueError("inventory record not found")
    if inventory.reserved_count >= inventory.total_count:
        session.rollback()
        raise ValueError("inventory is unavailable; escalation required")
    inventory.reserved_count += 1
    package = details["package"]
    list_price = Decimal(package["list_price"])
    discount = (
        (list_price - offered_price) / list_price * Decimal("100") if list_price else Decimal("0")
    )
    offer = Offer(
        lead_id=lead.id,
        context_version_id=context.id,
        package_id=package_id,
        list_price=list_price,
        offered_price=offered_price,
        discount_percent=discount,
        perks=perks,
        rationale=rationale,
        expires_at=utcnow() + timedelta(days=context.compiled["negotiation"]["offer_expiry_days"]),
    )
    session.add(offer)
    lead.state = "negotiating"
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="offer_created",
            data={
                "package_id": package_id,
                "offered_price": str(offered_price),
                "discount_percent": str(discount),
                "perks": perks,
                "context_version_id": context.id,
            },
        )
    )
    session.flush()
    session.refresh(offer)
    return offer


def queue_offer_message(session: Session, lead: EventLead, offer: Offer) -> ScheduledAction:
    existing = session.scalar(
        select(ScheduledAction).where(
            ScheduledAction.idempotency_key == f"offer:{offer.id}:send"
        )
    )
    if existing:
        return existing
    context = session.get(ContextVersion, offer.context_version_id)
    if not context:
        raise ValueError("offer context version not found")
    package = next(
        item for item in context.compiled["packages"] if item["id"] == offer.package_id
    )
    currency = context.compiled["negotiation"]["currency"]
    perks = ", ".join(offer.perks) if offer.perks else "the listed package benefits"
    body = (
        f"Based on our conversation, the sponsorship team can offer {package['name']} "
        f"at {currency} {offer.offered_price} including {perks}. "
        f"This offer is valid until {offer.expires_at.date().isoformat()}. "
        "Would you like us to reserve it and arrange the next step?"
    )
    conversation = ensure_conversation(session, lead)
    action = ScheduledAction(
        lead_id=lead.id,
        action_type="conversation_reply",
        channel=conversation.preferred_channel,
        due_at=utcnow(),
        idempotency_key=f"offer:{offer.id}:send",
        payload={"body": body, "offer_id": offer.id},
    )
    session.add(action)
    offer.status = "queued"
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="offer_message_queued",
            data={"offer_id": offer.id, "channel": conversation.preferred_channel},
        )
    )
    session.flush()
    return action


async def book_meeting(
    session: Session,
    adapters: AdapterRegistry,
    lead: EventLead,
    starts_at: datetime,
    timezone: str,
) -> Meeting:
    lead_id = lead.id
    key = f"meeting:{lead_id}:{starts_at.isoformat()}"
    contact = session.get(Contact, lead.contact_id)
    if not contact:
        raise ValueError("lead contact not found")
    invitee_name = contact.full_name
    invitee_email = contact.email_normalized
    # Persist the inbound/claim state and release any lead row lock before provider I/O.
    session.commit()
    result = await adapters.calendar.book(
        starts_at=starts_at,
        timezone=timezone,
        idempotency_key=key,
        invitee_name=invitee_name,
        invitee_email=invitee_email,
        lead_id=lead_id,
    )
    lead = session.scalar(
        select(EventLead)
        .where(EventLead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not lead:
        raise ValueError("lead disappeared while booking meeting")
    provider = adapters.calendar.name
    existing = session.scalar(
        select(Meeting).where(
            Meeting.provider == provider,
            Meeting.provider_booking_id == result.provider_booking_id,
        )
    )
    if existing:
        return existing
    meeting = Meeting(
        lead_id=lead.id,
        provider=provider,
        provider_booking_id=result.provider_booking_id,
        starts_at=result.starts_at,
        timezone=timezone,
        booking_url=result.booking_url,
    )
    session.add(meeting)
    if lead.state != "suppressed" and lead.automation_status == "active":
        lead.state = "call_booked"
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="meeting_booked",
            data={
                "starts_at": starts_at.isoformat(),
                "booking_url": result.booking_url,
                "lead_state": lead.state,
            },
        )
    )
    session.flush()
    return meeting


def queue_manual_reply(
    session: Session, lead: EventLead, channel: str, body: str, actor: str
) -> ScheduledAction:
    session.refresh(lead, with_for_update=True)
    cancel_pending_outreach(session, lead, "manual_takeover")
    lead.automation_status = "manual"
    key = f"lead:{lead.id}:manual:{datetime.now(UTC).timestamp()}"
    action = ScheduledAction(
        lead_id=lead.id,
        action_type="manual_reply",
        channel=channel,
        due_at=utcnow(),
        idempotency_key=key,
        payload={"body": body, "actor": actor},
    )
    session.add(action)
    audit(session, "conversation.manual_reply", "lead", lead.id, actor, {"channel": channel})
    session.commit()
    session.refresh(action)
    return action
