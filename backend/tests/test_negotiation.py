from datetime import UTC, datetime

from app.database import SessionLocal
from app.models import Offer
from sqlalchemy import select


def pin_lead(client, campaign, lead_id):
    response = client.post(
        f"/api/v1/leads/{lead_id}/workflow/start",
        json={"campaign_id": campaign["id"], "now": "2026-08-17T10:00:00Z"},
    )
    assert response.status_code == 200


def test_offer_engine_accepts_cap_and_rejects_below_floor(client, campaign, imported_lead):
    pin_lead(client, campaign, imported_lead["id"])
    accepted = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={
            "package_id": "gold",
            "offered_price": "9000",
            "perks": ["booth", "newsletter mention"],
            "rationale": "Approved floor",
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["discount_percent"] == "10.00"

    rejected = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={
            "package_id": "silver",
            "offered_price": "4000",
            "perks": ["guaranteed sales"],
        },
    )
    assert rejected.status_code == 422
    assert "below_minimum_price" in rejected.json()["detail"]
    assert "forbidden_or_unknown_perks" in rejected.json()["detail"]


def test_lead_stays_pinned_to_original_context(client, event, campaign, valid_documents, imported_lead):
    pin_lead(client, campaign, imported_lead["id"])
    pinned = client.get(f"/api/v1/leads/{imported_lead['id']}").json()["context_version_id"]
    changed = dict(valid_documents)
    changed["faq.md"] += "\nVersion two content."
    second = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate", json={"documents": changed}
    )
    assert second.status_code == 201
    assert second.json()["id"] != pinned
    assert client.get(f"/api/v1/leads/{imported_lead['id']}").json()["context_version_id"] == pinned



def test_offer_is_idempotent_and_context_versions_do_not_multiply_inventory(
    client, event, campaign, valid_documents, imported_lead
):
    pin_lead(client, campaign, imported_lead["id"])
    payload = {
        "package_id": "gold",
        "offered_price": "9000",
        "perks": ["booth"],
        "rationale": "Approved",
    }
    first = client.post(f"/api/v1/leads/{imported_lead['id']}/offers", json=payload)
    repeated = client.post(f"/api/v1/leads/{imported_lead['id']}/offers", json=payload)
    assert first.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["id"] == first.json()["id"]

    changed = dict(valid_documents)
    changed["faq.md"] += "\nA version two answer."
    context = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate", json={"documents": changed}
    ).json()
    created = client.post(
        f"/api/v1/events/{event['id']}/campaigns",
        json={
            "name": "Version two",
            "context_version_id": context["id"],
            "followup_days": [2, 5, 10],
            "whatsapp_fallback_day": 5,
        },
    ).json()
    active = client.post(f"/api/v1/campaigns/{created['id']}/activate").json()
    csv_data = (
        b"name,email,telegram,whatsapp,company,role,timezone,sponsor_answer\n"
        b"Second,second@example.com,second,,Example,Lead,UTC,yes\n"
        b"Third,third@example.com,third,,Example,Lead,UTC,yes\n"
    )
    imported = client.post(
        f"/api/v1/events/{event['id']}/imports",
        files={"file": ("more.csv", csv_data, "text/csv")},
    )
    assert imported.json()["eligible"] == 2
    leads = {
        lead["email"]: lead for lead in client.get(f"/api/v1/leads?event_id={event['id']}").json()
    }
    pin_lead(client, active, leads["second@example.com"]["id"])
    pin_lead(client, active, leads["third@example.com"]["id"])
    second = client.post(
        f"/api/v1/leads/{leads['second@example.com']['id']}/offers", json=payload
    )
    assert second.status_code == 201
    exhausted = client.post(
        f"/api/v1/leads/{leads['third@example.com']['id']}/offers", json=payload
    )
    assert exhausted.status_code == 422
    assert "inventory" in exhausted.json()["detail"]



def test_terminal_lead_state_settles_active_offer(client, campaign, imported_lead):
    pin_lead(client, campaign, imported_lead["id"])
    created = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={
            "package_id": "gold",
            "offered_price": "9000",
            "perks": ["booth"],
        },
    )
    assert created.status_code == 201
    terminal = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"state": "lost"}
    )
    assert terminal.status_code == 200
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert detail["offers"][0]["status"] == "lost"



def test_won_requires_selected_offer_and_releases_alternatives(client, campaign, imported_lead):
    pin_lead(client, campaign, imported_lead["id"])
    gold = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={"package_id": "gold", "offered_price": "9000", "perks": ["booth"]},
    ).json()
    silver = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={"package_id": "silver", "offered_price": "4500", "perks": ["logo"]},
    ).json()
    missing_selection = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"state": "won"}
    )
    assert missing_selection.status_code == 422
    won = client.patch(
        f"/api/v1/leads/{imported_lead['id']}",
        json={"state": "won", "accepted_offer_id": gold["id"]},
    )
    assert won.status_code == 200, won.text
    offers = {
        offer["id"]: offer
        for offer in client.get(f"/api/v1/leads/{imported_lead['id']}").json()["offers"]
    }
    assert offers[gold["id"]]["status"] == "accepted"
    assert offers[silver["id"]]["status"] == "declined"



def test_won_offer_requires_explicit_reopen_before_replacement(
    client, campaign, imported_lead
):
    pin_lead(client, campaign, imported_lead["id"])
    gold = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={"package_id": "gold", "offered_price": "9000", "perks": ["booth"]},
    ).json()
    won = client.patch(
        f"/api/v1/leads/{imported_lead['id']}",
        json={"state": "won", "accepted_offer_id": gold["id"]},
    )
    assert won.status_code == 200

    blocked = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={"package_id": "silver", "offered_price": "4500", "perks": ["logo"]},
    )
    assert blocked.status_code == 422
    assert "reopened" in blocked.json()["detail"]

    reopened = client.patch(
        f"/api/v1/leads/{imported_lead['id']}", json={"state": "engaged"}
    )
    assert reopened.status_code == 200
    offers = client.get(f"/api/v1/leads/{imported_lead['id']}").json()["offers"]
    assert next(offer for offer in offers if offer["id"] == gold["id"])["status"] == "reopened"


def test_forbidden_promise_and_mandatory_escalation_are_enforced(
    client, campaign, imported_lead
):
    pin_lead(client, campaign, imported_lead["id"])
    forbidden = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers",
        json={
            "package_id": "gold",
            "offered_price": "9000",
            "perks": ["booth"],
            "rationale": "This includes guaranteed---sales and legal   terms.",
        },
    )
    assert forbidden.status_code == 422
    assert "forbidden_promise" in forbidden.json()["detail"]
    assert "mandatory_escalation" in forbidden.json()["detail"]



def test_expired_offer_cannot_win_or_replay_idempotently(
    client, campaign, imported_lead
):
    pin_lead(client, campaign, imported_lead["id"])
    payload = {
        "package_id": "gold",
        "offered_price": "9000",
        "perks": ["booth"],
    }
    created = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers", json=payload
    ).json()
    with SessionLocal() as session:
        offer = session.scalar(select(Offer).where(Offer.id == created["id"]))
        assert offer is not None
        offer.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.commit()

    expired_win = client.patch(
        f"/api/v1/leads/{imported_lead['id']}",
        json={"state": "won", "accepted_offer_id": created["id"]},
    )
    assert expired_win.status_code == 422
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    expired = next(offer for offer in detail["offers"] if offer["id"] == created["id"])
    assert expired["status"] == "expired"

    replacement = client.post(
        f"/api/v1/leads/{imported_lead['id']}/offers", json=payload
    )
    assert replacement.status_code == 201
    assert replacement.json()["id"] != created["id"]
