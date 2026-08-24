import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters import (
    AdapterRegistry,
    AmbiguousProviderError,
    CalendarConflictError,
    RetryableProviderError,
    TerminalProviderError,
    registry,
)
from app.config import get_settings
from app.database import SessionLocal
from app.llm import FakeLLMProvider, LLMClient
from app.models import (
    ContextVersion,
    EventLead,
    Message,
    OutboxEvent,
    ProviderEvent,
    ScheduledAction,
)
from app.operations import handle_inbound_event
from app.research import research_lead
from app.workflows import dispatch_outbox, enqueue_due_actions
from conftest import csv_bytes
from sqlalchemy import select

NOW = "2026-08-17T10:00:00Z"


def start(client, lead_id, campaign_id):
    response = client.post(
        f"/api/v1/leads/{lead_id}/workflow/start",
        json={"campaign_id": campaign_id, "now": NOW},
    )
    assert response.status_code == 200, response.text


def test_fast_sequence_and_reply_atomically_cancel_pending(client, event, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    cycle = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T14:00:00Z"}
    )
    assert cycle.status_code == 200, cycle.text
    assert cycle.json()["dispatch"]["sent"] == 2

    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert {message["channel"] for message in detail["messages"]} == {"email", "telegram"}
    email = next(message for message in detail["messages"] if message["channel"] == "email")
    assert "Example Co" in email["body"]
    assert "Test Summit" in email["body"]
    assert email["provenance"]["composer"] == "llm"
    assert email["provenance"]["llm_provider"] == "fake-llm"
    assert len([action for action in detail["schedules"] if action["status"] == "pending"]) == 3

    inbound = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "tg-in-1",
            "channel": "telegram",
            "identity": "@avasponsor",
            "body": "I'm interested in Gold. Let's talk.",
            "occurred_at": "2026-08-17T11:00:00Z",
        },
    )
    assert inbound.status_code == 200, inbound.text
    assert inbound.json()["qualified"] is True
    assert inbound.json()["cancelled_actions"] == 3

    replay = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "tg-in-1",
            "channel": "telegram",
            "identity": "avasponsor",
            "body": "duplicate delivery",
        },
    )
    assert replay.json()["duplicate"] is True

    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert all(
        item["status"] != "pending" or item["type"] == "conversation_reply"
        for item in detail["schedules"]
    )


def test_telegram_never_admits_more_than_twenty_new_leads(client, event, campaign):
    rows = [
        (f"Lead {index}", f"lead{index}@example.com", f"lead{index}", "", "yes")
        for index in range(21)
    ]
    imported = client.post(
        f"/api/v1/events/{event['id']}/imports",
        files={"file": ("many.csv", csv_bytes(rows), "text/csv")},
    )
    assert imported.json()["eligible"] == 21
    leads = client.get(f"/api/v1/leads?event_id={event['id']}").json()
    for lead in leads:
        start(client, lead["id"], campaign["id"])

    cycle = client.post(
        "/api/v1/worker/run-due",
        json={"now": "2026-08-17T14:00:00Z", "limit": 100},
    ).json()
    assert cycle["enqueue"]["quota_deferred"] == 1
    assert cycle["dispatch"]["sent"] == 41  # 21 email + 20 Telegram
    analytics = client.get("/api/v1/analytics/overview").json()
    assert analytics["messages"]["outbound:telegram"] == 20


def test_opt_out_globally_suppresses_and_queues_no_reply(client, event, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    response = client.post(
        "/api/v1/inbound",
        json={
            "provider": "ses",
            "provider_event_id": "email-stop-1",
            "channel": "email",
            "identity": "ava@example.com",
            "body": "Unsubscribe",
        },
    )
    assert response.status_code == 200
    assert response.json()["suppressed"] is True
    assert response.json()["reply_queued"] is False
    assert client.get(f"/api/v1/leads/{imported_lead['id']}").json()["lead"]["state"] == "suppressed"



def test_call_request_slot_selection_books_meeting(client, event, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    first = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "call-request-1",
            "channel": "telegram",
            "identity": "avasponsor",
            "lead_id": imported_lead["id"],
            "body": "I am ready to jump on a call",
            "occurred_at": "2026-08-17T11:00:00Z",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["qualified"] is True
    second = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "slot-selection-1",
            "channel": "telegram",
            "identity": "avasponsor",
            "lead_id": imported_lead["id"],
            "body": "The second time works for me",
            "occurred_at": "2026-08-17T11:05:00Z",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["call_booked"] is True
    assert second.json()["coalesced_replies"] == 1
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["lead"]["state"] == "call_booked"
    assert len(detail["meetings"]) == 1


def test_delivery_event_is_deduplicated(client, event, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    client.post("/api/v1/worker/run-due", json={"now": "2026-08-17T14:00:00Z"})
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    sent = next(item for item in detail["messages"] if item["direction"] == "outbound")
    provider_id = sent["provider_message_id"]
    assert provider_id
    payload = {
        "provider": sent["provider"],
        "provider_event_id": "delivery-1",
        "provider_message_id": provider_id,
        "status": "delivered",
    }
    first = client.post("/api/v1/inbound/delivery", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False
    second = client.post("/api/v1/inbound/delivery", json=payload)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True



def test_workflow_rejects_campaign_from_another_event(
    client, event, campaign, imported_lead, valid_documents
):
    other_event = client.post(
        "/api/v1/events",
        json={"slug": "other-summit", "name": "Other Summit", "timezone": "UTC"},
    ).json()
    other_context = client.post(
        f"/api/v1/events/{other_event['id']}/contexts/activate",
        json={"documents": valid_documents},
    ).json()
    other_campaign = client.post(
        f"/api/v1/events/{other_event['id']}/campaigns",
        json={
            "name": "Other campaign",
            "context_version_id": other_context["id"],
            "followup_days": [2, 5, 10],
            "whatsapp_fallback_day": 5,
        },
    ).json()
    client.post(f"/api/v1/campaigns/{other_campaign['id']}/activate")
    response = client.post(
        f"/api/v1/leads/{imported_lead['id']}/workflow/start",
        json={"campaign_id": other_campaign["id"], "now": NOW},
    )
    assert response.status_code == 422
    assert "different events" in response.json()["detail"]


def test_manual_takeover_cancels_automated_actions(client, event, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    response = client.post(
        f"/api/v1/leads/{imported_lead['id']}/manual-reply",
        json={"channel": "email", "body": "A human response"},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    automated = [item for item in detail["schedules"] if item["type"] != "manual_reply"]
    assert automated
    assert all(item["status"] == "cancelled" for item in automated)
    assert all(item["cancelled_reason"] == "manual_takeover" for item in automated)



def test_pinned_qualification_policy_can_disable_automatic_booking(
    client, event, valid_documents, imported_lead
):
    documents = dict(valid_documents)
    documents["qualification.md"] = (
        "---\nexplicit_call_request_qualifies: false\n"
        "interest_plus_tier_qualifies: false\n---\nHuman qualification only."
    )
    context = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": documents},
    ).json()
    created = client.post(
        f"/api/v1/events/{event['id']}/campaigns",
        json={
            "name": "Human qualification",
            "context_version_id": context["id"],
            "followup_days": [2, 5, 10],
            "whatsapp_fallback_day": 5,
        },
    ).json()
    active = client.post(f"/api/v1/campaigns/{created['id']}/activate").json()
    start(client, imported_lead["id"], active["id"])
    response = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "policy-call-1",
            "channel": "telegram",
            "identity": "avasponsor",
            "lead_id": imported_lead["id"],
            "body": "I am ready to jump on a call",
        },
    )
    assert response.status_code == 200
    assert response.json()["qualified"] is False
    assert client.get(f"/api/v1/leads/{imported_lead['id']}").json()["lead"]["state"] == "escalated"



def test_non_utc_lead_waits_until_local_contact_window(client, event, campaign):
    content = csv_bytes(
        [("New York Lead", "ny@example.com", "nylead", "", "yes")]
    ).replace(b",UTC,yes", b",America/New_York,yes")
    imported = client.post(
        f"/api/v1/events/{event['id']}/imports",
        files={"file": ("ny.csv", content, "text/csv")},
    )
    assert imported.json()["eligible"] == 1
    lead = client.get(f"/api/v1/leads?event_id={event['id']}").json()[0]
    response = client.post(
        f"/api/v1/leads/{lead['id']}/workflow/start",
        json={"campaign_id": campaign["id"], "now": "2026-08-17T12:59:00Z"},
    )
    assert response.status_code == 200
    schedules = client.get(f"/api/v1/leads/{lead['id']}").json()["schedules"]
    email = next(item for item in schedules if item["type"] == "initial_email")
    telegram = next(item for item in schedules if item["type"] == "initial_telegram")
    email_due = datetime.fromisoformat(email["due_at"].replace("Z", "+00:00"))
    telegram_due = datetime.fromisoformat(telegram["due_at"].replace("Z", "+00:00"))
    email_due = email_due.replace(tzinfo=email_due.tzinfo or UTC)
    telegram_due = telegram_due.replace(tzinfo=telegram_due.tzinfo or UTC)
    assert datetime(2026, 8, 17, 13, 3, tzinfo=UTC) <= email_due
    assert email_due <= datetime(2026, 8, 17, 13, 28, tzinfo=UTC)
    assert timedelta(minutes=45) <= telegram_due - email_due <= timedelta(minutes=150)
    early = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T12:59:59Z"}
    ).json()
    assert early["dispatch"]["sent"] == 0
    open_window = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T16:00:00Z"}
    ).json()
    assert open_window["dispatch"]["sent"] == 2



def test_resume_reactivates_cancelled_action_and_outbox(client, event, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    with SessionLocal() as session:
        action = session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.lead_id == imported_lead["id"],
                ScheduledAction.action_type == "initial_email",
            )
        )
        assert action is not None
        action.status = "queued"
        session.add(
            OutboxEvent(
                aggregate_type="lead",
                aggregate_id=imported_lead["id"],
                event_type="message.send",
                idempotency_key=f"send:{action.id}",
                payload={
                    "action_id": action.id,
                    "lead_id": imported_lead["id"],
                    "channel": "email",
                    "identity": "ava@example.com",
                    "body": "Queued before takeover",
                    "context_version_id": campaign["context_version_id"],
                    "research_report_id": "test",
                },
            )
        )
        session.commit()
        action_id = action.id

    takeover = client.post(
        f"/api/v1/leads/{imported_lead['id']}/manual-reply",
        json={"channel": "email", "body": "Human takeover"},
    )
    assert takeover.status_code == 200
    resumed = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"automation_status": "active"}
    )
    assert resumed.status_code == 200
    start(client, imported_lead["id"], campaign["id"])

    with SessionLocal() as session:
        action = session.get(ScheduledAction, action_id)
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == f"send:{action_id}")
        )
        assert action is not None and action.status == "pending"
        assert outbox is not None and outbox.status == "pending"



def test_terminal_state_stops_and_cancels_outreach(client, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    terminal = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"state": "lost"}
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["automation_status"] == "stopped"

    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert all(action["status"] == "cancelled" for action in detail["schedules"])
    cycle = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T14:00:00Z"}
    )
    assert cycle.status_code == 200
    assert cycle.json()["dispatch"]["sent"] == 0


def test_workflow_start_replay_preserves_progress_and_actions(
    client, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    progressed = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"state": "qualified"}
    )
    assert progressed.status_code == 200

    start(client, imported_lead["id"], campaign["id"])
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["lead"]["state"] == "qualified"
    initial = [
        action
        for action in detail["schedules"]
        if action["type"] in {"initial_email", "initial_telegram"}
    ]
    assert len(initial) == 2
    started = [event for event in detail["timeline"] if event["type"] == "workflow_started"]
    assert len(started) == 1



def test_reopened_terminal_workflow_resumes_cancelled_actions(
    client, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    cycle = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T14:00:00Z"}
    )
    assert cycle.status_code == 200
    terminal = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"state": "unresponsive"}
    )
    assert terminal.status_code == 200
    reopened = client.patch(
        f"/api/v1/leads/{imported_lead['id']}",
        json={"state": "engaged", "automation_status": "active"},
    )
    assert reopened.status_code == 200, reopened.text

    start(client, imported_lead["id"], campaign["id"])
    schedules = client.get(f"/api/v1/leads/{imported_lead['id']}").json()["schedules"]
    followups = [
        action
        for action in schedules
        if action["type"] in {"followup", "whatsapp_fallback"}
    ]
    assert len(followups) == 3
    assert all(action["status"] == "pending" for action in followups)


def test_generic_suppressed_state_requires_global_suppression_endpoint(
    client, imported_lead
):
    response = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"state": "suppressed"}
    )
    assert response.status_code == 422
    assert "dedicated suppression endpoint" in response.json()["detail"]


def test_conversation_memory_tracks_topics_and_cross_channel_continuity(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    first = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "memory-question-1",
            "channel": "telegram",
            "identity": "avasponsor",
            "lead_id": imported_lead["id"],
            "body": "What is the package price?",
            "occurred_at": "2026-08-17T11:00:00Z",
        },
    )
    assert first.status_code == 200, first.text
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    first_memory = json.loads(detail["conversation"]["summary"])
    assert "packages" in first_memory["answered_questions"]
    assert first_memory["open_questions"] == []
    assert "via telegram" in first_memory["summary"]

    second = client.post(
        "/api/v1/inbound",
        json={
            "provider": "ses",
            "provider_event_id": "memory-interest-2",
            "channel": "email",
            "identity": "ava@example.com",
            "lead_id": imported_lead["id"],
            "body": "Silver looks relevant. I am interested; let's talk.",
            "occurred_at": "2026-08-17T11:03:00Z",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["coalesced_replies"] == 1
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    memory = json.loads(detail["conversation"]["summary"])
    assert memory["package_interest"] == ["silver"]
    assert "packages" in memory["answered_questions"]
    assert "via email" in memory["summary"]
    assert "Lead qualified for a sponsorship meeting" in memory["commitments"]
    memory_events = [
        item for item in detail["timeline"] if item["type"] == "conversation_memory_updated"
    ]
    assert len(memory_events) == 2


def test_outbound_generation_releases_lock_and_revalidates_cancelled_claim(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])

    class CancellingProvider(FakeLLMProvider):
        async def complete(self, **kwargs):
            with SessionLocal() as competing:
                claimed = competing.scalar(
                    select(ScheduledAction).where(
                        ScheduledAction.lead_id == imported_lead["id"],
                        ScheduledAction.action_type == "initial_email",
                        ScheduledAction.status == "generating",
                    )
                )
                assert claimed is not None
                claimed.status = "cancelled"
                claimed.cancelled_reason = "inbound_reply"
                competing.commit()
            return await super().complete(**kwargs)

    settings = get_settings()
    provider = CancellingProvider()
    with SessionLocal() as session:
        result = asyncio.run(
            enqueue_due_actions(
                session,
                datetime(2026, 8, 17, 10, 40, tzinfo=UTC),
                settings,
                brain=LLMClient(provider, settings),
            )
        )
    assert result["generation_discarded"] == 1
    assert result["queued"] == 0
    with SessionLocal() as session:
        email = session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.lead_id == imported_lead["id"],
                ScheduledAction.action_type == "initial_email",
            )
        )
        assert email is not None
        assert email.status == "cancelled"
        assert email.cancelled_reason == "inbound_reply"
        assert session.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == f"send:{email.id}")
        ) is None


def test_inbound_interpretation_releases_lock_and_discards_when_newer_message_arrives(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])

    class ConcurrentInboundProvider(FakeLLMProvider):
        async def complete(self, **kwargs):
            if kwargs.get("operation") == "interpret_reply":
                with SessionLocal() as competing:
                    event_claim = competing.scalar(
                        select(ProviderEvent).where(
                            ProviderEvent.provider == "telegram",
                            ProviderEvent.provider_event_id == "phase-race-1",
                        )
                    )
                    assert event_claim is not None
                    assert event_claim.payload["processing_status"] == "interpreting"
                    original = competing.scalar(
                        select(Message).where(
                            Message.provider_message_id == "phase-race-1"
                        )
                    )
                    assert original is not None
                    competing.add(
                        Message(
                            conversation_id=original.conversation_id,
                            direction="inbound",
                            channel="telegram",
                            provider="telegram",
                            body="A newer inbound turn",
                            provider_message_id="phase-race-newer",
                            provenance={"provider": "telegram"},
                        )
                    )
                    competing.commit()
            return await super().complete(**kwargs)

    settings = get_settings()
    with SessionLocal() as session:
        result = asyncio.run(
            handle_inbound_event(
                session,
                registry,
                provider="telegram",
                provider_event_id="phase-race-1",
                channel="telegram",
                identity="avasponsor",
                lead_id=imported_lead["id"],
                body="What does Silver cost?",
                occurred_at=datetime(2026, 8, 17, 11, 0, tzinfo=UTC),
                brain=LLMClient(ConcurrentInboundProvider(), settings),
            )
        )
    assert result["intent"] == "superseded"
    assert result["reply_queued"] is False
    with SessionLocal() as session:
        claim = session.scalar(
            select(ProviderEvent).where(
                ProviderEvent.provider == "telegram",
                ProviderEvent.provider_event_id == "phase-race-1",
            )
        )
        assert claim is not None
        assert claim.payload["processing_status"] == "superseded"
        assert claim.payload["superseded_reason"] == "newer_inbound"
        replies = session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.lead_id == imported_lead["id"],
                ScheduledAction.action_type == "conversation_reply",
            )
        ).all()
        assert replies == []


def test_conversation_generation_releases_lock_and_discards_stale_draft(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])

    class NewerDuringDraftProvider(FakeLLMProvider):
        async def complete(self, **kwargs):
            if kwargs.get("operation") == "conversation_reply":
                with SessionLocal() as competing:
                    event_claim = competing.scalar(
                        select(ProviderEvent).where(
                            ProviderEvent.provider == "telegram",
                            ProviderEvent.provider_event_id == "draft-race-1",
                        )
                    )
                    assert event_claim is not None
                    assert event_claim.payload["processing_status"] == "generating_reply"
                    original = competing.scalar(
                        select(Message).where(
                            Message.provider_message_id == "draft-race-1"
                        )
                    )
                    assert original is not None
                    competing.add(
                        Message(
                            conversation_id=original.conversation_id,
                            direction="inbound",
                            channel="telegram",
                            provider="telegram",
                            body="Newer context while the reply was being drafted",
                            provider_message_id="draft-race-newer",
                            provenance={"provider": "telegram"},
                        )
                    )
                    competing.commit()
            return await super().complete(**kwargs)

    settings = get_settings()
    with SessionLocal() as session:
        result = asyncio.run(
            handle_inbound_event(
                session,
                registry,
                provider="telegram",
                provider_event_id="draft-race-1",
                channel="telegram",
                identity="avasponsor",
                lead_id=imported_lead["id"],
                body="What is the package price?",
                occurred_at=datetime(2026, 8, 17, 11, 0, tzinfo=UTC),
                brain=LLMClient(NewerDuringDraftProvider(), settings),
            )
        )
    assert result["intent"] == "superseded"
    assert result["reply_queued"] is False
    with SessionLocal() as session:
        claim = session.scalar(
            select(ProviderEvent).where(
                ProviderEvent.provider == "telegram",
                ProviderEvent.provider_event_id == "draft-race-1",
            )
        )
        assert claim is not None
        assert claim.payload["processing_status"] == "reply_superseded"
        replies = session.scalars(
            select(ScheduledAction).where(
                ScheduledAction.lead_id == imported_lead["id"],
                ScheduledAction.action_type == "conversation_reply",
            )
        ).all()
        assert replies == []


def test_research_generation_releases_transaction_and_preserves_concurrent_suppression(
    client, event, campaign, imported_lead
):
    class SuppressingResearchProvider(FakeLLMProvider):
        async def complete(self, **kwargs):
            if kwargs.get("operation") == "research_synthesis":
                with SessionLocal() as competing:
                    lead = competing.get(EventLead, imported_lead["id"])
                    assert lead is not None
                    lead.state = "suppressed"
                    lead.automation_status = "stopped"
                    competing.commit()
            return await super().complete(**kwargs)

    settings = get_settings()
    with SessionLocal() as session:
        lead = session.get(EventLead, imported_lead["id"])
        context = session.get(ContextVersion, campaign["context_version_id"])
        report = asyncio.run(
            research_lead(
                session,
                lead,
                settings=settings,
                context=context,
                brain=LLMClient(SuppressingResearchProvider(), settings),
            )
        )
        session.commit()
        assert report.lead_id == imported_lead["id"]
    with SessionLocal() as session:
        lead = session.get(EventLead, imported_lead["id"])
        assert lead.state == "suppressed"
        assert lead.automation_status == "stopped"


def test_malformed_interpretation_escalates_without_reply(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    settings = get_settings()
    malformed = FakeLLMProvider(
        responses={"interpret_reply": ["not-json"] * (settings.llm_max_retries + 1)}
    )
    with SessionLocal() as session:
        result = asyncio.run(
            handle_inbound_event(
                session,
                registry,
                provider="telegram",
                provider_event_id="malformed-interpretation-1",
                channel="telegram",
                identity="avasponsor",
                lead_id=imported_lead["id"],
                body="Could you explain the packages?",
                occurred_at=datetime(2026, 8, 17, 11, 0, tzinfo=UTC),
                brain=LLMClient(malformed, settings),
            )
        )
    assert result["interpretation_failed"] is True
    assert result["reply_queued"] is False
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["lead"]["state"] == "escalated"
    assert not any(item["type"] == "conversation_reply" for item in detail["schedules"])
    assert any(
        item["type"] == "escalated"
        and item["data"].get("reason") == "reply_interpretation_failed"
        for item in detail["timeline"]
    )


def test_malformed_outreach_fails_action_without_creating_outbox(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    settings = get_settings()
    malformed = FakeLLMProvider(
        responses={"outreach": ["not-json"] * (settings.llm_max_retries + 1)}
    )
    with SessionLocal() as session:
        result = asyncio.run(
            enqueue_due_actions(
                session,
                datetime(2026, 8, 17, 10, 40, tzinfo=UTC),
                settings,
                brain=LLMClient(malformed, settings),
            )
        )
    assert result["llm_failed"] == 1
    assert result["queued"] == 0
    with SessionLocal() as session:
        action = session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.lead_id == imported_lead["id"],
                ScheduledAction.action_type == "initial_email",
            )
        )
        assert action.status == "failed"
        assert action.cancelled_reason == "llm_generation_failed"
        assert session.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == f"send:{action.id}")
        ) is None


def test_malformed_research_escalates_and_persists_no_report(
    client, event, campaign, imported_lead
):
    settings = get_settings()
    malformed = FakeLLMProvider(
        responses={"research_synthesis": ["not-json"] * (settings.llm_max_retries + 1)}
    )
    with SessionLocal() as session:
        lead = session.get(EventLead, imported_lead["id"])
        context = session.get(ContextVersion, campaign["context_version_id"])
        with pytest.raises(ValueError, match="research generation failed safely"):
            asyncio.run(
                research_lead(
                    session,
                    lead,
                    settings=settings,
                    context=context,
                    brain=LLMClient(malformed, settings),
                )
            )
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["lead"]["state"] == "escalated"
    assert detail["research"] == []
    assert any(
        item["type"] == "escalated"
        and item["data"].get("reason") == "research_generation_failed"
        for item in detail["timeline"]
    )


def test_ambiguous_send_is_reconcile_required_and_visible_in_crm(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    settings = get_settings()
    adapters = AdapterRegistry(settings)

    class AmbiguousEmailAdapter:
        name = "ambiguous-email"

        async def send(self, **kwargs):
            raise AmbiguousProviderError("provider acceptance is unknown")

    adapters.messaging["email"] = AmbiguousEmailAdapter()
    now = datetime(2026, 8, 17, 10, 40, tzinfo=UTC)
    with SessionLocal() as session:
        queued = asyncio.run(enqueue_due_actions(session, now, settings))
        assert queued["queued"] == 1
        dispatched = asyncio.run(dispatch_outbox(session, adapters, settings, now))
        assert dispatched["failed"] == 1
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    email = next(item for item in detail["schedules"] if item["type"] == "initial_email")
    assert email["status"] == "ambiguous"
    outbox = next(item for item in detail["outbox"] if item["status"] == "reconcile_required")
    assert outbox["attempts"] == 1
    assert "acceptance is unknown" in outbox["last_error"]
    analytics = client.get("/api/v1/analytics/overview").json()
    assert analytics["outbox_statuses"]["reconcile_required"] == 1


def test_calendar_booking_failure_escalates_without_false_confirmation(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    first = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "calendar-failure-call",
            "channel": "telegram",
            "identity": "avasponsor",
            "lead_id": imported_lead["id"],
            "body": "I am ready to jump on a call",
            "occurred_at": "2026-08-17T11:00:00Z",
        },
    )
    assert first.status_code == 200
    assert first.json()["qualified"] is True

    settings = get_settings()
    adapters = AdapterRegistry(settings)

    class FailingCalendar:
        name = "failing-calendar"

        async def book(self, **kwargs):
            raise TerminalProviderError("calendar unavailable")

        async def slots(self, **kwargs):
            return []

    adapters.calendar = FailingCalendar()
    with SessionLocal() as session:
        result = asyncio.run(
            handle_inbound_event(
                session,
                adapters,
                provider="telegram",
                provider_event_id="calendar-failure-selection",
                channel="telegram",
                identity="avasponsor",
                lead_id=imported_lead["id"],
                body="The second time works for me",
                occurred_at=datetime(2026, 8, 17, 11, 5, tzinfo=UTC),
            )
        )
    assert result["calendar_failed"] is True
    assert result["reply_queued"] is False
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["lead"]["state"] == "escalated"
    assert detail["meetings"] == []
    assert any(
        item["type"] == "escalated"
        and item["data"].get("reason") == "calendar_booking_failed"
        for item in detail["timeline"]
    )


def test_calendar_conflict_reoffers_authoritative_slots_without_false_booking(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    first = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "conflict-call",
            "channel": "telegram",
            "identity": "avasponsor",
            "lead_id": imported_lead["id"],
            "body": "I am ready to jump on a call",
            "occurred_at": "2026-08-17T11:00:00Z",
        },
    )
    assert first.json()["qualified"] is True
    settings = get_settings()
    adapters = AdapterRegistry(settings)
    replacement_slots = [
        datetime(2026, 8, 19, 15, 0, tzinfo=UTC),
        datetime(2026, 8, 19, 16, 0, tzinfo=UTC),
    ]

    class ConflictingCalendar:
        name = "conflicting-calendar"

        async def book(self, **kwargs):
            raise CalendarConflictError("slot taken")

        async def slots(self, **kwargs):
            return replacement_slots

    adapters.calendar = ConflictingCalendar()
    with SessionLocal() as session:
        result = asyncio.run(
            handle_inbound_event(
                session,
                adapters,
                provider="telegram",
                provider_event_id="conflict-selection",
                channel="telegram",
                identity="avasponsor",
                lead_id=imported_lead["id"],
                body="The second time works for me",
                occurred_at=datetime(2026, 8, 17, 11, 5, tzinfo=UTC),
            )
        )
    assert result["reply_queued"] is True
    assert result["call_booked"] is False
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["meetings"] == []
    assert detail["lead"]["state"] == "qualified"
    offered = next(
        item for item in detail["timeline"] if item["type"] == "meeting_slots_offered"
    )
    assert offered["data"]["slots"] == [slot.isoformat() for slot in replacement_slots]


def test_retryable_provider_failure_retries_same_outbox_without_duplicate_message(
    client, event, campaign, imported_lead
):
    start(client, imported_lead["id"], campaign["id"])
    settings = get_settings()
    adapters = AdapterRegistry(settings)
    successful = adapters.messaging["email"]

    class RetryOnceEmail:
        name = "retry-once-email"

        def __init__(self):
            self.calls = 0

        async def send(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RetryableProviderError("temporary", retry_after_seconds=30)
            return await successful.send(**kwargs)

    retrying = RetryOnceEmail()
    adapters.messaging["email"] = retrying
    now = datetime(2026, 8, 17, 10, 40, tzinfo=UTC)
    with SessionLocal() as session:
        queued = asyncio.run(enqueue_due_actions(session, now, settings))
        assert queued["queued"] == 1
        first = asyncio.run(dispatch_outbox(session, adapters, settings, now))
        assert first["failed"] == 1
        second = asyncio.run(
            dispatch_outbox(session, adapters, settings, now + timedelta(seconds=31))
        )
        assert second["sent"] == 1
    assert retrying.calls == 2
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    email_messages = [
        item
        for item in detail["messages"]
        if item["direction"] == "outbound" and item["channel"] == "email"
    ]
    assert len(email_messages) == 1
    email_outbox = next(item for item in detail["outbox"] if item["status"] == "processed")
    assert email_outbox["attempts"] == 1
