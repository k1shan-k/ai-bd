import json


def run_worker(client, at: str):
    response = client.post("/api/v1/worker/run-due", json={"now": at, "limit": 100})
    assert response.status_code == 200, response.text
    return response.json()


def test_csv_to_ai_outreach_cross_channel_qualification_and_booking(
    client, event, campaign, imported_lead
):
    started = client.post(
        f"/api/v1/leads/{imported_lead['id']}/workflow/start",
        json={"campaign_id": campaign["id"], "now": "2026-08-17T10:00:00Z"},
    )
    assert started.status_code == 200, started.text
    initial_cycle = run_worker(client, "2026-08-17T14:00:00Z")
    assert initial_cycle["dispatch"]["sent"] == 2

    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["context_version_id"] == campaign["context_version_id"]
    assert len(detail["research"]) == 1
    assert detail["research"][0]["provider"] == "fake"
    initial = [message for message in detail["messages"] if message["direction"] == "outbound"]
    assert {message["channel"] for message in initial} == {"email", "telegram"}
    assert all(message["provenance"]["composer"] == "llm" for message in initial)
    assert all("Test Summit" in message["body"] for message in initial)

    question = client.post(
        "/api/v1/inbound",
        json={
            "provider": "telegram",
            "provider_event_id": "e2e-question",
            "channel": "telegram",
            "identity": "avasponsor",
            "lead_id": imported_lead["id"],
            "body": "What does Silver cost?",
            "occurred_at": "2026-08-17T15:00:00Z",
        },
    )
    assert question.status_code == 200, question.text
    assert question.json()["classified_intent"] == "question"
    assert question.json()["reply_queued"] is True
    answer_cycle = run_worker(client, "2026-08-17T15:15:00Z")
    assert answer_cycle["dispatch"]["sent"] == 1

    call_request = client.post(
        "/api/v1/inbound",
        json={
            "provider": "ses",
            "provider_event_id": "e2e-call-request",
            "channel": "email",
            "identity": "ava@example.com",
            "lead_id": imported_lead["id"],
            "body": "Silver is relevant and I am ready to jump on a call.",
            "occurred_at": "2026-08-17T15:20:00Z",
        },
    )
    assert call_request.status_code == 200, call_request.text
    assert call_request.json()["qualified"] is True
    assert call_request.json()["reply_queued"] is True
    slot_cycle = run_worker(client, "2026-08-17T15:30:00Z")
    assert slot_cycle["dispatch"]["sent"] == 1

    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    slot_event = next(
        item for item in detail["timeline"] if item["type"] == "meeting_slots_offered"
    )
    assert len(slot_event["data"]["slots"]) >= 2
    memory = json.loads(detail["conversation"]["summary"])
    assert memory["package_interest"] == ["silver"]
    assert "packages" in memory["answered_questions"]
    assert "Lead qualified for a sponsorship meeting" in memory["commitments"]
    assert "via email" in memory["summary"]

    selection = client.post(
        "/api/v1/inbound",
        json={
            "provider": "ses",
            "provider_event_id": "e2e-slot-selection",
            "channel": "email",
            "identity": "ava@example.com",
            "lead_id": imported_lead["id"],
            "body": "The second time works for me.",
            "occurred_at": "2026-08-17T15:35:00Z",
        },
    )
    assert selection.status_code == 200, selection.text
    assert selection.json()["call_booked"] is True
    assert selection.json()["reply_queued"] is True
    confirmation_cycle = run_worker(client, "2026-08-17T15:45:00Z")
    assert confirmation_cycle["dispatch"]["sent"] == 1

    final = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert final["lead"]["state"] == "call_booked"
    assert len(final["meetings"]) == 1
    assert final["meetings"][0]["booking_url"]
    assert json.loads(final["conversation"]["summary"])["commitments"][-1] == (
        "Sponsorship meeting booked"
    )
    outbound = [message for message in final["messages"] if message["direction"] == "outbound"]
    assert len(outbound) == 5
    assert sum(message["provenance"].get("composer") == "llm" for message in outbound) == 4
    assert any("Confirmation:" in message["body"] for message in outbound)
    automated_outreach = [
        action
        for action in final["schedules"]
        if action["type"] in {"followup", "whatsapp_fallback"}
    ]
    assert automated_outreach
    assert all(action["status"] == "cancelled" for action in automated_outreach)
    assert all(item["status"] == "processed" for item in final["outbox"])
