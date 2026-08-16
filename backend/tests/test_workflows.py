
from app.database import SessionLocal
from app.models import OutboxEvent, ScheduledAction
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
        "/api/v1/worker/run-due", json={"now": "2026-08-17T10:10:00Z"}
    )
    assert cycle.status_code == 200, cycle.text
    assert cycle.json()["dispatch"]["sent"] == 2

    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert {message["channel"] for message in detail["messages"]} == {"email", "telegram"}
    email = next(message for message in detail["messages"] if message["channel"] == "email")
    assert "Explore how Example Co could reach the event audience" in email["body"]
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
        json={"now": "2026-08-17T10:10:00Z", "limit": 100},
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
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["lead"]["state"] == "call_booked"
    assert len(detail["meetings"]) == 1


def test_delivery_event_is_deduplicated(client, event, campaign, imported_lead):
    start(client, imported_lead["id"], campaign["id"])
    client.post("/api/v1/worker/run-due", json={"now": "2026-08-17T10:10:00Z"})
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    provider_id = detail["timeline"][0]["data"].get("provider_message_id")
    assert provider_id
    payload = {
        "provider": "fake",
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
    assert email["due_at"].startswith("2026-08-17T13:00:00")
    early = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T12:59:59Z"}
    ).json()
    assert early["dispatch"]["sent"] == 0
    open_window = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T13:10:00Z"}
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
        "/api/v1/worker/run-due", json={"now": "2026-08-17T10:10:00Z"}
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
        "/api/v1/worker/run-due", json={"now": "2026-08-17T10:10:00Z"}
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
