import json
from datetime import UTC, datetime

import pytest
from app.sns import parse_ses_received_email


def test_ses_receipt_mime_extracts_sender_body_and_exact_lead():
    lead_id = "123e4567-e89b-12d3-a456-426614174000"
    raw = (
        "From: Prospect <prospect@example.com>\r\n"
        f"To: replies+sponsorflow-{lead_id}@example.org\r\n"
        "Message-ID: <inbound-1@example.com>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Yes, please send the Gold package.\r\n"
    )
    parsed = parse_ses_received_email(
        {
            "notificationType": "Received",
            "mail": {
                "messageId": "ses-inbound-1",
                "source": "prospect@example.com",
                "destination": [f"replies+sponsorflow-{lead_id}@example.org"],
            },
            "content": raw,
        }
    )
    assert parsed == {
        "provider_event_id": "ses-inbound-1",
        "identity": "prospect@example.com",
        "lead_id": lead_id,
        "body": "Yes, please send the Gold package.",
    }


def test_ses_receipt_rejects_missing_raw_mime():
    with pytest.raises(ValueError, match="raw MIME"):
        parse_ses_received_email({"notificationType": "Received", "mail": {}})


def test_delivery_status_does_not_regress_and_permanent_bounce_suppresses(
    client, event, campaign, imported_lead
):
    start = client.post(
        f"/api/v1/leads/{imported_lead['id']}/workflow/start",
        json={"campaign_id": campaign["id"], "now": "2026-08-17T10:00:00Z"},
    )
    assert start.status_code == 200
    cycle = client.post(
        "/api/v1/worker/run-due", json={"now": "2026-08-17T10:10:00Z"}
    )
    assert cycle.status_code == 200
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    message = next(item for item in detail["messages"] if item["channel"] == "email")

    delivered = client.post(
        "/api/v1/inbound/delivery",
        json={
            "provider": "ses",
            "provider_event_id": "ses-delivered-1",
            "provider_message_id": message["provider_message_id"],
            "status": "delivered",
        },
    )
    assert delivered.status_code == 200
    accepted = client.post(
        "/api/v1/inbound/delivery",
        json={
            "provider": "ses",
            "provider_event_id": "ses-accepted-late",
            "provider_message_id": message["provider_message_id"],
            "status": "accepted",
        },
    )
    assert accepted.json()["ignored_regression"] is True
    assert accepted.json()["status"] == "delivered"

    bounced = client.post(
        "/api/v1/inbound/delivery",
        content=json.dumps(
            {
                "provider": "ses",
                "provider_event_id": "ses-bounce-1",
                "provider_message_id": message["provider_message_id"],
                "status": "bounced",
                "details": {"diagnostic": "Permanent"},
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    assert bounced.status_code == 200
    updated = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert updated["lead"]["state"] == "suppressed"


def test_telegram_replay_baselines_existing_history_then_replays_after_cursor():
    import asyncio
    from types import SimpleNamespace

    from app.adapters import TelegramMTProtoAdapter
    from app.config import Settings

    class FakeMessage:
        def __init__(self, message_id: int, text: str):
            self.id = message_id
            self.chat_id = 77
            self.raw_text = text
            self.date = datetime.now(UTC)
            self.out = False

        async def get_sender(self):
            return SimpleNamespace(username="pilot_user")

    class FakeClient:
        def __init__(self, messages):
            self.messages = messages

        async def iter_dialogs(self):
            yield SimpleNamespace(id=77, entity="dialog")

        async def iter_messages(self, _entity, *, limit, min_id=0, reverse=False):
            values = [message for message in self.messages if message.id > min_id]
            values.sort(key=lambda message: message.id, reverse=not reverse)
            for message in values[:limit]:
                yield message

    adapter = TelegramMTProtoAdapter(Settings())
    received = []

    async def handler(**event):
        received.append(event)

    async def baseline_cursor(_account_id, _chat_id):
        return None

    asyncio.run(
        adapter._replay_since_cursors(
            FakeClient([FakeMessage(10, "historical")]),
            "account-1",
            handler,
            baseline_cursor,
        )
    )
    assert [(event["message_id"], event["inbound"]) for event in received] == [(10, False)]

    received.clear()

    async def stored_cursor(_account_id, _chat_id):
        return 10

    asyncio.run(
        adapter._replay_since_cursors(
            FakeClient([FakeMessage(10, "old"), FakeMessage(11, "new")]),
            "account-1",
            handler,
            stored_cursor,
        )
    )
    assert received[0]["provider_event_id"] == "account-1:77:11"
    assert received[0]["identity"] == "pilot_user"
    assert received[0]["inbound"] is True
