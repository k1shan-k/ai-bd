import argparse
import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.adapters import TelegramMTProtoAdapter, registry
from app.database import SessionLocal, create_db_and_tables
from app.models import TelegramUpdateCursor, WorkerHeartbeat, utcnow
from app.operations import handle_inbound_event
from app.workflows import run_worker_cycle


def record_heartbeat(name: str, status: str = "running", details: dict | None = None) -> None:
    with SessionLocal() as session:
        row = session.get(WorkerHeartbeat, name)
        if not row:
            row = WorkerHeartbeat(name=name)
            session.add(row)
        row.heartbeat_at = utcnow()
        row.status = status
        row.details = details or {}
        session.commit()


def heartbeat_is_fresh(name: str, max_age_seconds: int = 60) -> bool:
    with SessionLocal() as session:
        row = session.get(WorkerHeartbeat, name)
        if not row:
            return False
        timestamp = row.heartbeat_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return (datetime.now(UTC) - timestamp).total_seconds() <= max_age_seconds


async def telegram_cursor(provider_account_id: str, chat_id: str) -> int | None:
    with SessionLocal() as session:
        row = session.get(TelegramUpdateCursor, (provider_account_id, chat_id))
        return row.message_id if row else None


def advance_telegram_cursor(
    session: Any,
    provider_account_id: str,
    chat_id: str,
    message_id: int,
) -> None:
    row = session.scalar(
        select(TelegramUpdateCursor)
        .where(
            TelegramUpdateCursor.provider_account_id == provider_account_id,
            TelegramUpdateCursor.chat_id == chat_id,
        )
        .with_for_update()
    )
    if not row:
        row = TelegramUpdateCursor(
            provider_account_id=provider_account_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        session.add(row)
    elif message_id > row.message_id:
        row.message_id = message_id
        row.updated_at = utcnow()
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        row = session.scalar(
            select(TelegramUpdateCursor)
            .where(
                TelegramUpdateCursor.provider_account_id == provider_account_id,
                TelegramUpdateCursor.chat_id == chat_id,
            )
            .with_for_update()
        )
        if not row:
            raise
        if message_id > row.message_id:
            row.message_id = message_id
            row.updated_at = utcnow()
        session.commit()


async def run_once(limit: int = 100) -> dict:
    if registry.settings.environment != "production":
        create_db_and_tables()
    with SessionLocal() as session:
        await registry.refresh(session)
        result = await run_worker_cycle(
            session, registry, registry.settings, datetime.now(UTC), limit=limit
        )
    record_heartbeat("worker", details=result)
    return result


async def telegram_listener() -> None:
    while True:
        owner_session = SessionLocal()
        lock_acquired = False
        try:
            if owner_session.bind and owner_session.bind.dialect.name == "postgresql":
                lock_acquired = bool(
                    owner_session.scalar(
                        text("SELECT pg_try_advisory_lock(hashtext('sponsorflow.telegram.listener'))")
                    )
                )
                if not lock_acquired:
                    owner_session.close()
                    await asyncio.sleep(30)
                    continue
            while True:
                record_heartbeat("telegram_listener")
                await registry.refresh(owner_session)
                adapter = registry.messaging.get("telegram")
                if not isinstance(adapter, TelegramMTProtoAdapter):
                    await asyncio.sleep(30)
                    continue

                async def receive(
                    *,
                    provider_event_id: str,
                    provider_account_id: str,
                    chat_id: str,
                    message_id: int,
                    identity: str | None,
                    body: str,
                    occurred_at: datetime,
                    inbound: bool,
                ) -> None:
                    with SessionLocal() as inbound_session:
                        if inbound and identity and body:
                            try:
                                await handle_inbound_event(
                                    inbound_session,
                                    registry,
                                    provider="telegram",
                                    provider_event_id=provider_event_id,
                                    channel="telegram",
                                    identity=identity,
                                    body=body,
                                    occurred_at=occurred_at,
                                )
                            except ValueError as exc:
                                inbound_session.rollback()
                                print({"telegram_inbound_rejected": str(exc)}, flush=True)
                        elif inbound:
                            print(
                                {"telegram_inbound_rejected": "message has no username or text"},
                                flush=True,
                            )
                        advance_telegram_cursor(
                            inbound_session,
                            provider_account_id,
                            chat_id,
                            message_id,
                        )

                await adapter.listen(receive, telegram_cursor)
        except Exception as exc:
            print({"telegram_listener_error": str(exc)}, flush=True)
            await asyncio.sleep(30)
        finally:
            if lock_acquired:
                try:
                    owner_session.execute(
                        text("SELECT pg_advisory_unlock(hashtext('sponsorflow.telegram.listener'))")
                    )
                except Exception:
                    pass
            owner_session.close()


async def serve(interval: int, limit: int) -> None:
    listener = asyncio.create_task(telegram_listener())
    try:
        while True:
            result = await run_once(limit)
            print(result, flush=True)
            await asyncio.sleep(interval)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="SponsorFlow durable action worker")
    parser.add_argument("--once", action="store_true", help="process one cycle and exit")
    parser.add_argument(
        "--healthcheck", action="store_true", help="exit successfully when worker heartbeat is fresh"
    )
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.healthcheck:
        raise SystemExit(0 if heartbeat_is_fresh("worker") else 1)
    if args.once:
        print(asyncio.run(run_once(args.limit)))
    else:
        asyncio.run(serve(args.interval, args.limit))


if __name__ == "__main__":
    main()
