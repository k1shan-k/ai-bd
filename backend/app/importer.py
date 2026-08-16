import csv
import hashlib
import io
import json
import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Contact, EventLead, ImportJob, ImportRow, SuppressionEntry
from app.schemas import ImportMapping, ImportPreview, ImportSummary

ELIGIBLE_ANSWERS = {"yes", "maybe"}


def normalize_answer(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    return normalized if normalized in ELIGIBLE_ANSWERS else "no"


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_telegram(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://t\.me/", "", value)
    return value.lstrip("@").split("?")[0].rstrip("/")


def normalize_phone(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    prefix = "+" if value.startswith("+") else ""
    digits = re.sub(r"\D", "", value)
    return f"{prefix}{digits}" if len(digits) >= 7 else None


def valid_email(value: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", value))


def detect_mapping(headers: list[str]) -> ImportMapping:
    normalized = {header.lower().strip().replace(" ", "_"): header for header in headers}

    def pick(*candidates: str) -> str | None:
        return next((normalized[c] for c in candidates if c in normalized), None)

    return ImportMapping(
        full_name=pick("name", "full_name", "full_name") or "name",
        email=pick("email", "email_address") or "email",
        telegram=pick("telegram", "telegram_username", "tg") or "telegram",
        whatsapp=pick("whatsapp", "whatsapp_number", "phone"),
        company=pick("company", "company_name", "organization"),
        role=pick("role", "job_title", "title"),
        timezone=pick("timezone", "time_zone", "tz"),
        sponsor_answer=pick(
            "sponsor_answer", "sponsorship_interest", "do_you_want_to_sponsor"
        )
        or "sponsor_answer",
    )


def parse_csv(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    rows = [dict(row) for row in reader]
    return list(reader.fieldnames), rows


def preview_csv(content: bytes) -> ImportPreview:
    headers, rows = parse_csv(content)
    return ImportPreview(
        headers=headers,
        sample=rows[:5],
        detected_mapping=detect_mapping(headers),
        file_hash=hashlib.sha256(content).hexdigest(),
    )


def _get(row: dict[str, str], column: str | None) -> str:
    return (row.get(column, "") if column else "") or ""


def normalize_row(row: dict[str, str], mapping: ImportMapping) -> dict[str, Any]:
    return {
        "full_name": " ".join(_get(row, mapping.full_name).strip().split()),
        "email": normalize_email(_get(row, mapping.email)),
        "telegram": normalize_telegram(_get(row, mapping.telegram)),
        "whatsapp": normalize_phone(_get(row, mapping.whatsapp)),
        "company": " ".join(_get(row, mapping.company).strip().split()) or None,
        "role": " ".join(_get(row, mapping.role).strip().split()) or None,
        "timezone": _get(row, mapping.timezone).strip() or "UTC",
        "sponsor_answer": normalize_answer(_get(row, mapping.sponsor_answer)),
    }


def _suppressed(session: Session, data: dict[str, Any]) -> bool:
    identities = [("email", data["email"]), ("telegram", data["telegram"])]
    if data["whatsapp"]:
        identities.append(("whatsapp", data["whatsapp"]))
    return (
        session.scalar(
            select(SuppressionEntry.id).where(
                or_(
                    *[
                        (SuppressionEntry.identity_type == kind)
                        & (SuppressionEntry.identity_value == value)
                        for kind, value in identities
                    ]
                )
            )
        )
        is not None
    )


def import_csv(
    session: Session,
    event_id: str,
    file_name: str,
    content: bytes,
    mapping: ImportMapping,
) -> ImportSummary:
    file_hash = hashlib.sha256(content).hexdigest()
    existing = session.scalar(
        select(ImportJob).where(ImportJob.event_id == event_id, ImportJob.file_hash == file_hash)
    )
    if existing:
        return ImportSummary(import_job_id=existing.id, **existing.summary)

    headers, rows = parse_csv(content)
    required_columns = {mapping.full_name, mapping.email, mapping.telegram, mapping.sponsor_answer}
    missing = required_columns - set(headers)
    if missing:
        raise ValueError(f"CSV is missing mapped columns: {', '.join(sorted(missing))}")

    job = ImportJob(
        event_id=event_id,
        file_name=file_name,
        file_hash=file_hash,
        mapping=mapping.model_dump(),
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        session.expire_all()
        winner = session.scalar(
            select(ImportJob).where(
                ImportJob.event_id == event_id,
                ImportJob.file_hash == file_hash,
            )
        )
        if winner:
            return ImportSummary(import_job_id=winner.id, **winner.summary)
        raise
    counts = {key: 0 for key in ImportSummary.model_fields if key != "import_job_id"}

    for row_number, row in enumerate(rows, start=2):
        data = normalize_row(row, mapping)
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()
        outcome = "invalid"
        reason: str | None = None

        if not data["full_name"] or not valid_email(data["email"]) or not data["telegram"]:
            reason = "name, valid email, and Telegram are required"
        elif data["sponsor_answer"] not in {"yes", "maybe"}:
            outcome, reason = "ineligible", "sponsor answer is not yes/maybe"
        elif _suppressed(session, data):
            outcome, reason = "suppressed", "identity is globally suppressed"
        else:
            email_contact = session.scalar(
                select(Contact).where(Contact.email_normalized == data["email"])
            )
            telegram_contact = session.scalar(
                select(Contact).where(Contact.telegram_normalized == data["telegram"])
            )
            phone_contacts = (
                session.scalars(
                    select(Contact).where(Contact.whatsapp_normalized == data["whatsapp"])
                ).all()
                if data["whatsapp"]
                else []
            )
            primary_ids = {item.id for item in (email_contact, telegram_contact) if item}
            if len(primary_ids) > 1:
                outcome, reason = "quarantined", "email and Telegram resolve to different contacts"
            else:
                contact = email_contact or telegram_contact
                phone_conflict = any(not contact or item.id != contact.id for item in phone_contacts)
                primary_conflict = contact and (
                    contact.email_normalized != data["email"]
                    or contact.telegram_normalized != data["telegram"]
                )
                existing_phone_conflict = (
                    contact
                    and contact.whatsapp_normalized
                    and data["whatsapp"]
                    and contact.whatsapp_normalized != data["whatsapp"]
                )
                if primary_conflict:
                    outcome, reason = "quarantined", "identity conflicts with an existing contact"
                elif phone_conflict or existing_phone_conflict:
                    outcome, reason = (
                        "quarantined",
                        "WhatsApp supporting identity conflicts with an existing contact",
                    )
                else:
                    if not contact:
                        candidate = Contact(
                            full_name=data["full_name"],
                            email_normalized=data["email"],
                            telegram_normalized=data["telegram"],
                            whatsapp_normalized=data["whatsapp"],
                            company_name=data["company"],
                            role=data["role"],
                            timezone=data["timezone"],
                        )
                        try:
                            with session.begin_nested():
                                session.add(candidate)
                                session.flush()
                            contact = candidate
                        except IntegrityError:
                            session.expire_all()
                            email_winner = session.scalar(
                                select(Contact).where(Contact.email_normalized == data["email"])
                            )
                            telegram_winner = session.scalar(
                                select(Contact).where(
                                    Contact.telegram_normalized == data["telegram"]
                                )
                            )
                            if (
                                email_winner
                                and telegram_winner
                                and email_winner.id == telegram_winner.id
                            ):
                                contact = email_winner
                            else:
                                outcome, reason = (
                                    "quarantined",
                                    "identity was concurrently claimed by conflicting contacts",
                                )
                    if contact and outcome != "quarantined":
                        contact = session.scalar(
                            select(Contact)
                            .where(Contact.id == contact.id)
                            .with_for_update()
                        )
                        if not contact:
                            outcome, reason = "quarantined", "contact disappeared during import"
                        elif _suppressed(session, data):
                            outcome, reason = "suppressed", "identity is globally suppressed"
                        else:
                            if not contact.whatsapp_normalized and data["whatsapp"]:
                                contact.whatsapp_normalized = data["whatsapp"]
                            lead = session.scalar(
                                select(EventLead).where(
                                    EventLead.event_id == event_id,
                                    EventLead.contact_id == contact.id,
                                )
                            )
                            if lead:
                                outcome, reason = "duplicate", "lead already exists for this event"
                            else:
                                candidate_lead = EventLead(
                                    event_id=event_id,
                                    contact_id=contact.id,
                                    sponsor_answer=data["sponsor_answer"],
                                    state="eligible",
                                )
                                try:
                                    with session.begin_nested():
                                        session.add(candidate_lead)
                                        session.flush()
                                    outcome = "eligible"
                                except IntegrityError:
                                    outcome, reason = (
                                        "duplicate",
                                        "lead was concurrently created for this event",
                                    )

        counts[outcome] += 1
        session.add(
            ImportRow(
                import_job_id=job.id,
                row_number=row_number,
                row_fingerprint=fingerprint,
                raw_data=row,
                normalized_data=data,
                outcome=outcome,
                reason=reason,
            )
        )

    job.summary = counts
    job.status = "completed"
    session.commit()
    return ImportSummary(import_job_id=job.id, **counts)
