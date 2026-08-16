import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from app.adapters import registry
from app.config import get_settings
from app.database import SessionLocal, engine
from app.importer import import_csv
from app.models import EventLead, PackageInventory
from app.operations import (
    create_offer,
    handle_inbound_event,
    queue_offer_message,
    suppress_contact,
)
from app.policy import reserve_telegram_new_contact
from app.schemas import ImportMapping
from sqlalchemy import select

POSTGRES_ONLY = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="requires PostgreSQL row locks"
)


@POSTGRES_ONLY
def test_postgres_serializes_twenty_contact_quota_under_concurrency():
    barrier = Barrier(25)

    def reserve() -> bool:
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            allowed, _count = reserve_telegram_new_contact(
                session,
                date(2026, 8, 17),
                get_settings().telegram_daily_new_contact_limit,
            )
            session.commit()
            return allowed

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(lambda _index: reserve(), range(25)))
    assert sum(results) == 20


@POSTGRES_ONLY
def test_concurrent_same_file_imports_return_one_claim(client, event):
    content = (
        b"name,email,telegram,whatsapp,company,role,timezone,sponsor_answer\n"
        b"Concurrent,concurrent@example.com,concurrent,,Example,Lead,UTC,yes\n"
    )
    barrier = Barrier(2)

    def run_import():
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            return import_csv(
                session,
                event["id"],
                "same.csv",
                content,
                ImportMapping(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: run_import(), range(2)))
    assert results[0].import_job_id == results[1].import_job_id
    assert results[0].eligible == results[1].eligible == 1
    assert len(client.get(f"/api/v1/leads?event_id={event['id']}").json()) == 1


@POSTGRES_ONLY
def test_concurrent_provider_replay_claim_has_one_winner(client, imported_lead):
    barrier = Barrier(2)

    def deliver():
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            return asyncio.run(
                handle_inbound_event(
                    session,
                    registry,
                    provider="telegram",
                    provider_event_id="concurrent-provider-event",
                    channel="telegram",
                    identity="avasponsor",
                    lead_id=imported_lead["id"],
                    body="A message that needs human review",
                )
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: deliver(), range(2)))
    assert sorted(result["duplicate"] for result in results) == [False, True]
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    inbound = [message for message in detail["messages"] if message["direction"] == "inbound"]
    assert len(inbound) == 1


@POSTGRES_ONLY
def test_concurrent_identical_offer_reserves_once(
    client, campaign, imported_lead
):
    started = client.post(
        f"/api/v1/leads/{imported_lead['id']}/workflow/start",
        json={"campaign_id": campaign["id"], "now": "2026-08-17T10:00:00Z"},
    )
    assert started.status_code == 200
    barrier = Barrier(2)

    def propose():
        with SessionLocal() as session:
            lead = session.get(EventLead, imported_lead["id"])
            assert lead is not None
            barrier.wait(timeout=10)
            offer = create_offer(
                session,
                lead,
                "gold",
                Decimal("9000"),
                ["booth"],
                "Concurrent identical proposal",
            )
            queue_offer_message(session, lead, offer)
            session.commit()
            return offer.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        offer_ids = list(pool.map(lambda _index: propose(), range(2)))
    assert offer_ids[0] == offer_ids[1]
    with SessionLocal() as session:
        inventory = session.scalar(
            select(PackageInventory).where(
                PackageInventory.event_id == event_id_for(session, imported_lead["id"]),
                PackageInventory.package_id == "gold",
            )
        )
        assert inventory.reserved_count == 1


def event_id_for(session, lead_id: str) -> str:
    lead = session.get(EventLead, lead_id)
    assert lead is not None
    return lead.event_id



@POSTGRES_ONLY
def test_suppression_and_import_share_contact_lock(client, imported_lead):
    other_event = client.post(
        "/api/v1/events",
        json={"slug": "suppression-race", "name": "Suppression Race", "timezone": "UTC"},
    ).json()
    content = (
        b"name,email,telegram,whatsapp,company,role,timezone,sponsor_answer\n"
        b"Ava Sponsor,ava@example.com,avasponsor,+15550000001,Example Co,Partnerships,UTC,yes\n"
    )
    barrier = Barrier(2)

    def suppress():
        with SessionLocal() as session:
            lead = session.get(EventLead, imported_lead["id"])
            assert lead is not None
            barrier.wait(timeout=10)
            suppress_contact(session, lead, "concurrent_suppression")
            session.commit()

    def import_other_event():
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            return import_csv(
                session,
                other_event["id"],
                "suppression-race.csv",
                content,
                ImportMapping(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        suppress_future = pool.submit(suppress)
        import_future = pool.submit(import_other_event)
        suppress_future.result(timeout=20)
        import_future.result(timeout=20)

    with SessionLocal() as session:
        original = session.get(EventLead, imported_lead["id"])
        assert original is not None
        leads = session.scalars(
            select(EventLead).where(EventLead.contact_id == original.contact_id)
        ).all()
        assert leads
        assert all(lead.state == "suppressed" for lead in leads)
        assert all(lead.automation_status == "stopped" for lead in leads)
