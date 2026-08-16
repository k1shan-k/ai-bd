from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters import AdapterRegistry, CalendarConflictError
from app.importer import normalize_email, normalize_phone, normalize_telegram
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
            ScheduledAction.status.in_(["pending", "queued"]),
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


def _classify(body: str, context: ContextVersion | None) -> tuple[str, str | None]:
    text = " ".join(body.lower().split())
    opt_out_phrases = {
        "stop",
        "unsubscribe",
        "remove me",
        "do not contact me",
        "don't contact me",
        "not interested",
        "no thanks",
        "wrong person",
    }
    rejection_markers = [
        "unsubscribe",
        "do not contact",
        "don't contact",
        "remove me",
        "not interested",
        "no thanks",
        "wrong person",
    ]
    if text in opt_out_phrases or any(phrase in text for phrase in rejection_markers):
        return "opt_out", None
    if any(term in text for term in ["jump on a call", "book a call", "schedule a call", "let's talk", "lets talk", "ready to talk"]):
        return "call_request", None
    package_match = None
    if context:
        for package in context.compiled.get("packages", []):
            if package["id"].lower() in text or package["name"].lower() in text:
                package_match = package["id"]
                break
    interested = any(
        term in text
        for term in ["interested", "sounds good", "tell me more", "send details", "sponsor"]
    )
    if interested and package_match:
        return "interested_with_tier", package_match
    if interested:
        return "interested", None
    if "?" in body or any(term in text for term in ["price", "cost", "package", "benefit", "perk"]):
        return "question", package_match
    return "uncertain", None


def _package_answer(context: ContextVersion) -> str:
    currency = context.compiled.get("negotiation", {}).get("currency", "")
    items = [
        f"{package['name']} ({currency} {package['list_price']})"
        for package in context.compiled.get("packages", [])
    ]
    return (
        "Thanks for asking. The currently available sponsorship options are: "
        + "; ".join(items)
        + ". Tell us which is closest to your goals and we can share the included benefits."
    )


def _queue_conversation_reply(
    session: Session,
    lead: EventLead,
    channel: str,
    body: str,
    event_key: str,
    now: datetime,
) -> None:
    session.add(
        ScheduledAction(
            lead_id=lead.id,
            action_type="conversation_reply",
            channel=channel,
            due_at=now,
            idempotency_key=f"lead:{lead.id}:conversation_reply:{event_key}",
            payload={"body": body, "source_event": event_key},
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
    session.add(
        Message(
            conversation_id=conversation.id,
            direction="inbound",
            channel=channel,
            provider=provider,
            body=body,
            provider_message_id=provider_event_id,
            provenance={"provider": provider},
        )
    )
    cancelled = cancel_pending_outreach(session, lead, "inbound_reply")
    previous_state = lead.state
    lead.last_reply_at = now
    lead.state = "engaged"
    context = session.get(ContextVersion, lead.context_version_id) if lead.context_version_id else None
    selected_slot = _select_offered_slot(session, lead, body) if previous_state == "qualified" else None
    intent, tier = ("meeting_selection", None) if selected_slot else _classify(body, context)
    qualification = context.compiled.get("qualification", {}) if context else {}
    qualification_allowed = (
        intent == "call_request"
        and qualification.get("explicit_call_request_qualifies", False) is True
    ) or (
        intent == "interested_with_tier"
        and qualification.get("interest_plus_tier_qualifies", False) is True
    )
    response: str | None = None

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
            response = (
                f"You're booked for {meeting.starts_at.isoformat()}. "
                f"Confirmation: {meeting.booking_url}"
            )
        except CalendarConflictError:
            lead.state = "qualified"
            slots = await adapters.calendar.slots(after=now, timezone=contact.timezone)
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
    elif intent in {"call_request", "interested_with_tier"} and qualification_allowed:
        lead.state = "qualified"
        lead.qualified_at = now
        slots = await adapters.calendar.slots(after=now, timezone=contact.timezone)
        formatted = ", ".join(slot.isoformat() for slot in slots)
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="meeting_slots_offered",
                data={"slots": [slot.isoformat() for slot in slots], "timezone": contact.timezone},
            )
        )
        response = (
            "Absolutely — we'd be happy to talk. Here are the next available times: "
            f"{formatted}. Reply with the one that works best for you."
        )
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
    elif intent == "interested":
        response = (
            "Great to hear. Which sponsorship option is closest to what you have in mind? "
            "We can also share a concise comparison of the available tiers."
        )
    elif intent == "question" and context:
        response = _package_answer(context)
    else:
        lead.state = "escalated"
        session.add(
            TimelineEvent(
                lead_id=lead.id,
                event_type="escalated",
                data={"reason": "low_confidence_inbound", "body": body},
            )
        )

    if response and lead.state != "suppressed":
        _queue_conversation_reply(session, lead, channel, response, provider_event_id, now)
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="message_received",
            actor_type="prospect",
            data={
                "channel": channel,
                "provider_event_id": provider_event_id,
                "intent": intent,
                "tier": tier,
                "cancelled_actions": cancelled,
            },
        )
    )
    session.commit()
    return {
        "duplicate": False,
        "lead_id": lead.id,
        "intent": intent,
        "qualified": lead.state in {"qualified", "call_booked"},
        "call_booked": lead.state == "call_booked",
        "suppressed": lead.state == "suppressed",
        "cancelled_actions": cancelled,
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
    key = f"meeting:{lead.id}:{starts_at.isoformat()}"
    contact = session.get(Contact, lead.contact_id)
    if not contact:
        raise ValueError("lead contact not found")
    result = await adapters.calendar.book(
        starts_at=starts_at,
        timezone=timezone,
        idempotency_key=key,
        invitee_name=contact.full_name,
        invitee_email=contact.email_normalized,
        lead_id=lead.id,
    )
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
    lead.state = "call_booked"
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="meeting_booked",
            data={"starts_at": starts_at.isoformat(), "booking_url": result.booking_url},
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
