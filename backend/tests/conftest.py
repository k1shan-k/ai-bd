import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path(__file__).parent / "test.db"
os.environ["SPONSORFLOW_ENVIRONMENT"] = "test"
os.environ.setdefault("SPONSORFLOW_DATABASE_URL", f"sqlite:///{TEST_DB}")
os.environ["SPONSORFLOW_PROVIDER_MODE"] = "fake"

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    TEST_DB.unlink(missing_ok=True)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def valid_documents():
    return {
        "company.md": "---\nname: Test Org\n---\nWe run events.",
        "voice-and-style.md": "---\npersona: sponsorship team\n---\nBe useful.",
        "event.md": "---\nname: Test Summit\ntimezone: UTC\n---\nA focused summit.",
        "audience.md": "---\nexpected_attendance: 200\n---\nTechnology leaders.",
        "packages.md": "---\npackages:\n  - id: gold\n    name: Gold\n    list_price: 10000\n    min_price: 9000\n    perks: [booth, logo]\n  - id: silver\n    name: Silver\n    list_price: 5000\n    min_price: 4500\n    perks: [logo]\n---\nApproved tiers.",
        "negotiation-policy.md": "---\ncurrency: USD\nmax_discount_percent: 10\nallowed_custom_perks: [newsletter mention]\nforbidden_promises: [guaranteed sales]\nmandatory_escalation: [legal terms]\noffer_expiry_days: 7\n---\nStay in bounds.",
        "inventory.md": "---\ninventory:\n  gold: 2\n  silver: 4\n---\nAvailable inventory.",
        "faq.md": "---\nowner: sponsorship team\n---\nApproved answers.",
        "qualification.md": "---\nexplicit_call_request_qualifies: true\ninterest_plus_tier_qualifies: true\n---\nQualification.",
        "escalation.md": "---\nrules: [low confidence, legal, complaint]\n---\nEscalate safely.",
    }


@pytest.fixture
def event(client):
    response = client.post(
        "/api/v1/events",
        json={"slug": "test-summit", "name": "Test Summit", "timezone": "UTC"},
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def campaign(client, event, valid_documents):
    context = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": valid_documents},
    )
    assert context.status_code == 201, context.text
    campaign = client.post(
        f"/api/v1/events/{event['id']}/campaigns",
        json={
            "name": "Fast sequence",
            "context_version_id": context.json()["id"],
            "followup_days": [2, 5, 10],
            "whatsapp_fallback_day": 5,
        },
    )
    assert campaign.status_code == 201
    active = client.post(f"/api/v1/campaigns/{campaign.json()['id']}/activate")
    assert active.status_code == 200
    return active.json()


def csv_bytes(rows: list[tuple[str, str, str, str, str]]) -> bytes:
    lines = ["name,email,telegram,whatsapp,company,role,timezone,sponsor_answer"]
    for name, email, telegram, whatsapp, answer in rows:
        lines.append(
            f"{name},{email},{telegram},{whatsapp},Example Co,Partnerships,UTC,{answer}"
        )
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def imported_lead(client, event):
    content = csv_bytes([("Ava Sponsor", "ava@example.com", "@avasponsor", "+15550000001", "yes")])
    response = client.post(
        f"/api/v1/events/{event['id']}/imports",
        files={"file": ("leads.csv", content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    leads = client.get(f"/api/v1/leads?event_id={event['id']}").json()
    return leads[0]
