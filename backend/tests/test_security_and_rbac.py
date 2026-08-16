import hashlib
import hmac
import json
import time


def test_viewer_cannot_write(client):
    response = client.post(
        "/api/v1/events",
        headers={"X-Actor": "readonly", "X-Role": "viewer"},
        json={"slug": "blocked-event", "name": "Blocked Event"},
    )
    assert response.status_code == 403


def test_invalid_role_is_rejected(client):
    response = client.get("/api/v1/events", headers={"X-Role": "superuser"})
    assert response.status_code == 403


def test_configured_management_key_ignores_forged_role_header(client, monkeypatch):
    from app.main import settings

    monkeypatch.setattr(settings, "admin_api_key", "real-admin-key")
    denied = client.get("/api/v1/events", headers={"X-Role": "admin"})
    assert denied.status_code == 401
    allowed = client.get("/api/v1/events", headers={"X-API-Key": "real-admin-key"})
    assert allowed.status_code == 200


def test_provider_webhook_requires_fresh_body_signature(client, monkeypatch):
    from app.main import settings

    secret = "webhook-secret"
    monkeypatch.setattr(settings, "inbound_webhook_token", secret)
    payload = {
        "provider": "telegram",
        "provider_event_id": "forged-event",
        "channel": "telegram",
        "identity": "unknown",
        "body": "stop",
    }
    denied = client.post("/api/v1/inbound", json=payload)
    assert denied.status_code == 401

    raw = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    authenticated = client.post(
        "/api/v1/inbound",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": f"sha256={signature}",
        },
        content=raw,
    )
    assert authenticated.status_code == 422
    assert "no contact" in authenticated.json()["detail"]

    stale_timestamp = str(int(time.time()) - 301)
    stale_signature = hmac.new(
        secret.encode(), stale_timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    stale = client.post(
        "/api/v1/inbound",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": stale_timestamp,
            "X-Webhook-Signature": stale_signature,
        },
        content=raw,
    )
    assert stale.status_code == 401
    assert "stale" in stale.json()["detail"]
