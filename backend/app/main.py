import csv
import hashlib
import hmac
import io
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters import registry
from app.config import get_settings
from app.context import activate_context, compile_context
from app.database import create_db_and_tables, get_session
from app.importer import import_csv, preview_csv
from app.models import (
    AuditEvent,
    Campaign,
    Contact,
    ContextVersion,
    Conversation,
    Event,
    EventLead,
    ImportJob,
    ImportRow,
    Meeting,
    Message,
    Offer,
    OutboxEvent,
    ProviderConfig,
    ResearchReport,
    ScheduledAction,
    SuppressionEntry,
    TelegramDailyQuota,
    TimelineEvent,
    WorkerHeartbeat,
)
from app.operations import (
    audit,
    book_meeting,
    cancel_pending_outreach,
    create_offer,
    handle_calendar_event,
    handle_delivery_event,
    handle_inbound_event,
    queue_manual_reply,
    queue_offer_message,
    settle_terminal_offers,
    suppress_contact,
)
from app.provider_config import (
    PROVIDERS,
    decrypt_secrets,
    list_provider_configs,
    mark_provider_check,
    serialize_provider,
    update_provider_config,
    write_provider_secrets,
)
from app.research import research_lead
from app.schemas import (
    CampaignCreate,
    CampaignLaunch,
    CampaignRead,
    ContextActivate,
    ContextRead,
    DeliveryEventRequest,
    EventCreate,
    EventRead,
    ImportMapping,
    ImportPreview,
    ImportSummary,
    InboundEventRequest,
    LeadRead,
    LeadUpdate,
    ManualReplyRequest,
    MeetingRequest,
    OfferRequest,
    ProviderCheckRead,
    ProviderConfigRead,
    ProviderConfigUpdate,
    ResearchRead,
    ResearchRequest,
    SuppressRequest,
    TelegramAuthConfirm,
    WorkflowRunDue,
    WorkflowStart,
)
from app.sns import (
    confirm_sns_subscription,
    parse_ses_received_email,
    verify_sns_signature,
)
from app.workflows import run_worker_cycle, start_lead_workflow

app = FastAPI(
    title="SponsorFlow API",
    version="0.1.0",
    description="Policy-controlled multi-channel sponsorship business development platform",
)
settings = get_settings()
VALID_LEAD_STATES = {
    "eligible",
    "researching",
    "ready",
    "email_sent",
    "telegram_queued",
    "active_outreach",
    "followup_due",
    "whatsapp_fallback",
    "engaged",
    "qualified",
    "negotiating",
    "escalated",
    "call_booked",
    "won",
    "lost",
    "unresponsive",
    "suppressed",
}
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    if settings.environment != "production":
        create_db_and_tables()


def actor_context(
    x_api_key: Annotated[str | None, Header()] = None,
    x_actor: Annotated[str | None, Header()] = None,
    x_role: Annotated[str | None, Header()] = None,
) -> tuple[str, str]:
    configured = {
        "admin": settings.admin_api_key,
        "operator": settings.operator_api_key,
        "viewer": settings.viewer_api_key,
    }
    for role, expected in configured.items():
        if expected and x_api_key and secrets.compare_digest(x_api_key, expected):
            return (x_actor or f"{role}-api-client", role)
    if any(configured.values()) or settings.environment == "production":
        raise HTTPException(401, "a valid management API key is required")
    # Local development/test mode has no external exposure; role headers support RBAC tests only.
    local_role = x_role or "admin"
    if local_role not in {"admin", "operator", "viewer"}:
        raise HTTPException(403, "unknown role")
    return (x_actor or "local-operator", local_role)


async def require_webhook_signature(
    request: Request,
    x_webhook_timestamp: Annotated[str | None, Header()] = None,
    x_webhook_signature: Annotated[str | None, Header()] = None,
) -> None:
    secret = settings.inbound_webhook_token
    if not secret:
        if settings.environment == "production":
            raise HTTPException(503, "provider webhook authentication is not configured")
        return
    if not x_webhook_timestamp or not x_webhook_signature:
        raise HTTPException(401, "signed provider webhook headers are required")
    try:
        timestamp = int(x_webhook_timestamp)
    except ValueError as exc:
        raise HTTPException(401, "invalid webhook timestamp") from exc
    if abs(int(time.time()) - timestamp) > 300:
        raise HTTPException(401, "stale provider webhook")
    raw_body = await request.body()
    signed = x_webhook_timestamp.encode() + b"." + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    supplied = x_webhook_signature.removeprefix("sha256=")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid provider webhook signature")


def require_write(actor: tuple[str, str] = Depends(actor_context)) -> tuple[str, str]:
    if actor[1] not in {"admin", "operator"}:
        raise HTTPException(403, "write access requires operator or admin role")
    return actor


def require_admin(actor: tuple[str, str] = Depends(actor_context)) -> tuple[str, str]:
    if actor[1] != "admin":
        raise HTTPException(403, "admin role required")
    return actor


def get_or_404(session: Session, model, object_id: str):
    obj = session.get(model, object_id)
    if not obj:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


def as_http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def require_live_provider_readiness(session: Session) -> None:
    await registry.refresh(session)
    if settings.provider_mode == "fake":
        return
    if settings.environment == "production":
        now = datetime.now(UTC)
        for heartbeat_name in ("worker", "telegram_listener"):
            heartbeat = session.get(WorkerHeartbeat, heartbeat_name)
            if not heartbeat:
                raise HTTPException(503, f"{heartbeat_name} has not reported ready")
            reported_at = heartbeat.heartbeat_at
            if reported_at.tzinfo is None:
                reported_at = reported_at.replace(tzinfo=UTC)
            if (now - reported_at).total_seconds() > 60:
                raise HTTPException(503, f"{heartbeat_name} heartbeat is stale")
    missing = registry.configuration_errors()
    if missing:
        raise HTTPException(503, "live providers are missing configuration: " + ", ".join(missing))
    checks = await registry.checks()
    unavailable = [item["provider"] for item in checks if not item["configured"]]
    if unavailable:
        raise HTTPException(503, "live provider checks failed: " + ", ".join(unavailable))


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    try:
        session.scalar(select(func.count()).select_from(ProviderConfig))
    except Exception as exc:
        raise HTTPException(503, "database or migrations are not ready") from exc
    return {"status": "ok", "service": "sponsorflow", "provider_mode": settings.provider_mode}


@app.get(f"{settings.api_prefix}/providers/checks", response_model=list[ProviderCheckRead])
async def provider_checks(
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    await registry.refresh(session)
    return await registry.checks()


@app.get(
    f"{settings.api_prefix}/admin/providers",
    response_model=list[ProviderConfigRead],
)
def admin_provider_configs(
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(require_admin),
):
    try:
        return list_provider_configs(session, settings)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.put(
    f"{settings.api_prefix}/admin/providers/{{provider}}",
    response_model=ProviderConfigRead,
)
async def save_provider_config(
    provider: str,
    body: ProviderConfigUpdate,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_admin),
):
    try:
        row = update_provider_config(
            session,
            settings,
            provider,
            enabled=body.enabled,
            config=body.config,
            supplied_secrets=body.secrets,
            clear_secrets=body.clear_secrets,
            expected_revision=body.expected_revision,
            actor=actor[0],
        )
    except ValueError as exc:
        raise as_http_error(exc) from exc
    audit(
        session,
        "provider.config.update",
        "provider",
        provider,
        actor[0],
        {
            "revision": row.revision,
            "enabled": row.enabled,
            "config_fields": sorted(body.config),
            "secret_fields_changed": sorted(
                set(body.secrets) | set(body.clear_secrets)
            ),
        },
    )
    session.commit()
    await registry.refresh(session, force=True)
    return serialize_provider(settings, row, provider)


@app.post(f"{settings.api_prefix}/admin/providers/{{provider}}/check")
async def check_provider_config(
    provider: str,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_admin),
):
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    row = session.get(ProviderConfig, provider)
    if not row or not row.enabled:
        raise HTTPException(409, "save and enable this provider before testing")
    try:
        await registry.refresh(session, force=True)
        checks = await registry.checks()
        match = next((item for item in checks if item["provider"] == provider), None)
        if provider == "tavily":
            match = next(
                (item for item in checks if item["provider"] == registry.settings.research_provider),
                None,
            )
        details = (match or {}).get("details", {"reason": "provider check unavailable"})
        ok = bool(match and match.get("configured"))
        mark_provider_check(row, ok=ok, details=details)
        audit(
            session,
            "provider.check",
            "provider",
            provider,
            actor[0],
            {"ok": ok, "revision": row.revision},
        )
        session.commit()
        return {
            "provider": provider,
            "configured": ok,
            "details": row.last_check_details,
        }
    except ValueError as exc:
        raise as_http_error(exc) from exc


@app.post(f"{settings.api_prefix}/admin/providers/telegram/auth/start")
async def start_telegram_auth(
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_admin),
):
    row = session.scalar(
        select(ProviderConfig)
        .where(ProviderConfig.provider == "telegram")
        .with_for_update()
    )
    if not row:
        raise HTTPException(409, "save the Telegram API ID, phone, and API hash first")
    provider_secrets = decrypt_secrets(settings, row)
    api_id = row.config.get("api_id")
    phone = row.config.get("phone")
    api_hash = provider_secrets.get("api_hash")
    if not api_id or not phone or not api_hash:
        raise HTTPException(409, "Telegram API ID, phone, and API hash are required")
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise HTTPException(503, "Telethon is not installed in the API image") from exc
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(str(phone))
        provider_secrets.update(
            {
                "pending_session_string": client.session.save(),
                "pending_phone_code_hash": str(sent.phone_code_hash),
            }
        )
        write_provider_secrets(settings, row, provider_secrets)
        row.revision += 1
        row.updated_by = actor[0]
        audit(
            session,
            "provider.telegram.auth.start",
            "provider",
            "telegram",
            actor[0],
            {"revision": row.revision},
        )
        session.commit()
    finally:
        await client.disconnect()
    return {"code_sent": True, "phone": str(phone), "revision": row.revision}


@app.post(f"{settings.api_prefix}/admin/providers/telegram/auth/confirm")
async def confirm_telegram_auth(
    body: TelegramAuthConfirm,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_admin),
):
    row = session.scalar(
        select(ProviderConfig)
        .where(ProviderConfig.provider == "telegram")
        .with_for_update()
    )
    if not row:
        raise HTTPException(409, "Telegram authentication has not started")
    provider_secrets = decrypt_secrets(settings, row)
    pending_session = provider_secrets.get("pending_session_string")
    phone_code_hash = provider_secrets.get("pending_phone_code_hash")
    api_hash = provider_secrets.get("api_hash")
    phone = row.config.get("phone")
    api_id = row.config.get("api_id")
    if not all([pending_session, phone_code_hash, api_hash, phone, api_id]):
        raise HTTPException(409, "request a fresh Telegram login code")
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise HTTPException(503, "Telethon is not installed in the API image") from exc
    client = TelegramClient(StringSession(pending_session), int(api_id), api_hash)
    try:
        await client.connect()
        try:
            await client.sign_in(
                phone=str(phone),
                code=body.code,
                phone_code_hash=phone_code_hash,
            )
        except Exception as exc:
            if exc.__class__.__name__ != "SessionPasswordNeededError":
                raise HTTPException(422, f"Telegram login failed: {exc}") from exc
            if not body.password:
                return {"authenticated": False, "password_required": True}
            await client.sign_in(password=body.password)
        if not await client.is_user_authorized():
            raise HTTPException(422, "Telegram did not authorize the session")
        provider_secrets["session_string"] = client.session.save()
        provider_secrets.pop("pending_session_string", None)
        provider_secrets.pop("pending_phone_code_hash", None)
        write_provider_secrets(settings, row, provider_secrets)
        row.enabled = True
        row.revision += 1
        row.updated_by = actor[0]
        audit(
            session,
            "provider.telegram.auth.complete",
            "provider",
            "telegram",
            actor[0],
            {"revision": row.revision},
        )
        session.commit()
    finally:
        await client.disconnect()
    await registry.refresh(session, force=True)
    return {"authenticated": True, "password_required": False, "revision": row.revision}


@app.post(f"{settings.api_prefix}/events", response_model=EventRead, status_code=201)
def create_event(
    body: EventCreate,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    if session.scalar(select(Event).where(Event.slug == body.slug)):
        raise HTTPException(409, "event slug already exists")
    if body.starts_at and body.outreach_cutoff_at and body.outreach_cutoff_at > body.starts_at:
        raise HTTPException(422, "outreach cutoff must not be after event start")
    event = Event(**body.model_dump())
    session.add(event)
    session.flush()
    audit(session, "event.create", "event", event.id, actor[0])
    session.commit()
    session.refresh(event)
    return event


@app.get(f"{settings.api_prefix}/events", response_model=list[EventRead])
def list_events(
    session: Session = Depends(get_session), _actor: tuple[str, str] = Depends(actor_context)
):
    return session.scalars(select(Event).order_by(Event.created_at.desc())).all()


@app.post(f"{settings.api_prefix}/events/{{event_id}}/contexts/validate")
def validate_context(
    event_id: str,
    body: ContextActivate,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(require_write),
):
    get_or_404(session, Event, event_id)
    compiled, errors = compile_context(body.documents)
    return {"valid": not errors, "errors": errors, "compiled": compiled}


@app.post(
    f"{settings.api_prefix}/events/{{event_id}}/contexts/activate",
    response_model=ContextRead,
    status_code=201,
)
def activate_event_context(
    event_id: str,
    body: ContextActivate,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    event = get_or_404(session, Event, event_id)
    try:
        version = activate_context(session, event, body.documents, actor[0])
    except ValueError as exc:
        raise as_http_error(exc) from exc
    audit(session, "context.activate", "context_version", version.id, actor[0])
    session.commit()
    session.refresh(version)
    return version


@app.get(f"{settings.api_prefix}/events/{{event_id}}/contexts", response_model=list[ContextRead])
def list_contexts(
    event_id: str,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    return session.scalars(
        select(ContextVersion)
        .where(ContextVersion.event_id == event_id)
        .order_by(ContextVersion.version.desc())
    ).all()


@app.post(
    f"{settings.api_prefix}/events/{{event_id}}/campaigns",
    response_model=CampaignRead,
    status_code=201,
)
def create_campaign(
    event_id: str,
    body: CampaignCreate,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    get_or_404(session, Event, event_id)
    context = get_or_404(session, ContextVersion, body.context_version_id)
    if context.event_id != event_id:
        raise HTTPException(422, "context version belongs to another event")
    if sorted(set(body.followup_days)) != body.followup_days or any(
        day <= 0 for day in body.followup_days
    ):
        raise HTTPException(422, "followup_days must be unique, positive, and sorted")
    campaign = Campaign(event_id=event_id, **body.model_dump())
    session.add(campaign)
    session.flush()
    audit(session, "campaign.create", "campaign", campaign.id, actor[0])
    session.commit()
    session.refresh(campaign)
    return campaign


@app.get(f"{settings.api_prefix}/events/{{event_id}}/campaigns", response_model=list[CampaignRead])
def list_campaigns(
    event_id: str,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    get_or_404(session, Event, event_id)
    return session.scalars(
        select(Campaign)
        .where(Campaign.event_id == event_id)
        .order_by(Campaign.created_at.desc())
    ).all()


@app.post(f"{settings.api_prefix}/campaigns/{{campaign_id}}/activate", response_model=CampaignRead)
def activate_campaign(
    campaign_id: str,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    campaign = get_or_404(session, Campaign, campaign_id)
    campaign.status = "active"
    audit(session, "campaign.activate", "campaign", campaign.id, actor[0])
    session.commit()
    session.refresh(campaign)
    return campaign


@app.post(f"{settings.api_prefix}/imports/preview", response_model=ImportPreview)
async def preview_import(
    file: UploadFile = File(...), _actor: tuple[str, str] = Depends(require_write)
):
    try:
        return preview_csv(await file.read())
    except (UnicodeDecodeError, ValueError) as exc:
        raise as_http_error(ValueError(str(exc))) from exc


@app.post(f"{settings.api_prefix}/events/{{event_id}}/imports", response_model=ImportSummary)
async def execute_import(
    event_id: str,
    file: UploadFile = File(...),
    mapping: str | None = Form(None),
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    get_or_404(session, Event, event_id)
    try:
        parsed_mapping = ImportMapping.model_validate_json(mapping) if mapping else ImportMapping()
        result = import_csv(
            session, event_id, file.filename or "upload.csv", await file.read(), parsed_mapping
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise as_http_error(ValueError(str(exc))) from exc
    audit(session, "import.complete", "import_job", result.import_job_id, actor[0])
    session.commit()
    return result


@app.get(f"{settings.api_prefix}/events/{{event_id}}/imports")
def list_imports(
    event_id: str,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    get_or_404(session, Event, event_id)
    jobs = session.scalars(
        select(ImportJob)
        .where(ImportJob.event_id == event_id)
        .order_by(ImportJob.created_at.desc())
    ).all()
    return [
        {
            "id": job.id,
            "file_name": job.file_name,
            "file_hash": job.file_hash,
            "mapping": job.mapping,
            "summary": job.summary,
            "status": job.status,
            "created_at": job.created_at,
        }
        for job in jobs
    ]


@app.get(f"{settings.api_prefix}/imports/{{import_id}}/rows")
def list_import_rows(
    import_id: str,
    outcome: str | None = None,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    get_or_404(session, ImportJob, import_id)
    statement = select(ImportRow).where(ImportRow.import_job_id == import_id)
    if outcome:
        statement = statement.where(ImportRow.outcome == outcome)
    rows = session.scalars(statement.order_by(ImportRow.row_number)).all()
    return [
        {
            "id": row.id,
            "row_number": row.row_number,
            "raw_data": row.raw_data,
            "normalized_data": row.normalized_data,
            "outcome": row.outcome,
            "reason": row.reason,
        }
        for row in rows
    ]


def lead_to_read(lead: EventLead, contact: Contact) -> LeadRead:
    return LeadRead(
        id=lead.id,
        full_name=contact.full_name,
        email=contact.email_normalized,
        telegram=contact.telegram_normalized,
        whatsapp=contact.whatsapp_normalized,
        company=contact.company_name,
        role=contact.role,
        sponsor_answer=lead.sponsor_answer,
        state=lead.state,
        delivery_state=lead.delivery_state,
        automation_status=lead.automation_status,
        created_at=lead.created_at,
    )


@app.get(f"{settings.api_prefix}/leads", response_model=list[LeadRead])
def list_leads(
    event_id: str | None = None,
    state: str | None = None,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    statement = select(EventLead, Contact).join(Contact, Contact.id == EventLead.contact_id)
    if event_id:
        statement = statement.where(EventLead.event_id == event_id)
    if state:
        statement = statement.where(EventLead.state == state)
    return [lead_to_read(lead, contact) for lead, contact in session.execute(statement).all()]


@app.get(f"{settings.api_prefix}/leads/{{lead_id}}")
def get_lead(
    lead_id: str,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    lead = get_or_404(session, EventLead, lead_id)
    contact = get_or_404(session, Contact, lead.contact_id)
    conversation = session.scalar(select(Conversation).where(Conversation.lead_id == lead.id))
    messages = (
        session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
        ).all()
        if conversation
        else []
    )
    timeline = session.scalars(
        select(TimelineEvent)
        .where(TimelineEvent.lead_id == lead.id)
        .order_by(TimelineEvent.created_at.desc())
    ).all()
    schedules = session.scalars(
        select(ScheduledAction)
        .where(ScheduledAction.lead_id == lead.id)
        .order_by(ScheduledAction.due_at)
    ).all()
    reports = session.scalars(
        select(ResearchReport)
        .where(ResearchReport.lead_id == lead.id)
        .order_by(ResearchReport.created_at.desc())
    ).all()
    offers = session.scalars(select(Offer).where(Offer.lead_id == lead.id)).all()
    meetings = session.scalars(select(Meeting).where(Meeting.lead_id == lead.id)).all()
    return {
        "lead": lead_to_read(lead, contact).model_dump(mode="json"),
        "event_id": lead.event_id,
        "campaign_id": lead.campaign_id,
        "context_version_id": lead.context_version_id,
        "conversation": {
            "status": conversation.status,
            "preferred_channel": conversation.preferred_channel,
            "summary": conversation.summary,
        }
        if conversation
        else None,
        "messages": [
            {
                "id": item.id,
                "direction": item.direction,
                "channel": item.channel,
                "provider": item.provider,
                "provider_message_id": item.provider_message_id,
                "body": item.body,
                "provenance": item.provenance,
                "created_at": item.created_at,
            }
            for item in messages
        ],
        "timeline": [
            {
                "id": item.id,
                "type": item.event_type,
                "actor_type": item.actor_type,
                "data": item.data,
                "created_at": item.created_at,
            }
            for item in timeline
        ],
        "schedules": [
            {
                "id": item.id,
                "type": item.action_type,
                "channel": item.channel,
                "due_at": item.due_at,
                "status": item.status,
                "cancelled_reason": item.cancelled_reason,
            }
            for item in schedules
        ],
        "research": [
            ResearchRead.model_validate(item).model_dump(mode="json") for item in reports
        ],
        "offers": [
            {
                "id": item.id,
                "package_id": item.package_id,
                "list_price": str(item.list_price),
                "offered_price": str(item.offered_price),
                "discount_percent": str(item.discount_percent),
                "perks": item.perks,
                "status": item.status,
            }
            for item in offers
        ],
        "meetings": [
            {
                "id": item.id,
                "starts_at": item.starts_at,
                "timezone": item.timezone,
                "status": item.status,
                "booking_url": item.booking_url,
            }
            for item in meetings
        ],
    }


@app.patch(f"{settings.api_prefix}/leads/{{lead_id}}", response_model=LeadRead)
def update_lead(
    lead_id: str,
    body: LeadUpdate,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    lead = get_or_404(session, EventLead, lead_id)
    session.refresh(lead, with_for_update=True)
    if body.state:
        if body.state not in VALID_LEAD_STATES:
            raise HTTPException(422, "unknown lead pipeline state")
        if body.state == "suppressed":
            raise HTTPException(422, "use the dedicated suppression endpoint")
        try:
            settle_terminal_offers(session, lead, body.state, body.accepted_offer_id)
        except ValueError as exc:
            raise as_http_error(exc) from exc
        lead.state = body.state
        if body.state in {"won", "lost", "unresponsive", "suppressed"}:
            cancel_pending_outreach(session, lead, f"lead_{body.state}")
            lead.automation_status = "stopped"
    if body.automation_status:
        if body.automation_status == "active" and lead.state in {
            "won",
            "lost",
            "unresponsive",
            "suppressed",
        }:
            raise HTTPException(422, "terminal leads must be reopened before automation resumes")
        if body.automation_status in {"paused", "manual", "stopped"}:
            cancel_pending_outreach(session, lead, f"operator_{body.automation_status}")
        lead.automation_status = body.automation_status
    session.add(
        TimelineEvent(
            lead_id=lead.id,
            event_type="operator_update",
            actor_type="operator",
            actor_id=actor[0],
            data=body.model_dump(exclude_none=True),
        )
    )
    audit(session, "lead.update", "lead", lead.id, actor[0], body.model_dump(exclude_none=True))
    session.commit()
    return lead_to_read(lead, get_or_404(session, Contact, lead.contact_id))


@app.post(f"{settings.api_prefix}/leads/{{lead_id}}/suppress")
def suppress_lead(
    lead_id: str,
    body: SuppressRequest,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    lead = get_or_404(session, EventLead, lead_id)
    suppress_contact(session, lead, body.reason, actor[0])
    session.commit()
    return {"lead_id": lead.id, "suppressed": True, "scope": "global"}


@app.post(f"{settings.api_prefix}/leads/{{lead_id}}/research", response_model=ResearchRead)
def research(
    lead_id: str,
    body: ResearchRequest,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    lead = get_or_404(session, EventLead, lead_id)
    if settings.environment == "production" and body.provider == "fake":
        raise HTTPException(409, "fake research is disabled in production")
    try:
        report = research_lead(session, lead, body.provider, registry.settings)
    except ValueError as exc:
        raise as_http_error(exc) from exc
    audit(session, "research.complete", "lead", lead.id, actor[0], {"provider": body.provider})
    session.commit()
    session.refresh(report)
    return report


@app.post(f"{settings.api_prefix}/campaigns/{{campaign_id}}/launch")
async def launch_campaign(
    campaign_id: str,
    body: CampaignLaunch,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    await require_live_provider_readiness(session)
    campaign = get_or_404(session, Campaign, campaign_id)
    statement = select(EventLead).where(
        EventLead.event_id == campaign.event_id,
        EventLead.sponsor_answer.in_(["yes", "maybe"]),
        EventLead.automation_status == "active",
        EventLead.delivery_state == "not_started",
    )
    if body.lead_ids:
        statement = statement.where(EventLead.id.in_(body.lead_ids))
    leads = session.scalars(statement).all()
    launched: list[str] = []
    skipped: list[dict[str, str]] = []
    launch_time = body.now or datetime.now(UTC)
    for lead in leads:
        try:
            with session.begin_nested():
                start_lead_workflow(
                    session, lead, campaign, launch_time, registry.settings
                )
            launched.append(lead.id)
        except ValueError as exc:
            skipped.append({"lead_id": lead.id, "reason": str(exc)})
    audit(
        session,
        "campaign.launch",
        "campaign",
        campaign.id,
        actor[0],
        {"launched": len(launched), "skipped": len(skipped)},
    )
    session.commit()
    return {"campaign_id": campaign.id, "launched": launched, "skipped": skipped}


@app.post(f"{settings.api_prefix}/campaigns/{{campaign_id}}/simulate")
async def simulate_campaign(
    campaign_id: str,
    body: CampaignLaunch,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    if settings.provider_mode != "fake":
        raise HTTPException(409, "accelerated simulation is available only with fake providers")
    campaign = get_or_404(session, Campaign, campaign_id)
    statement = select(EventLead).where(
        EventLead.event_id == campaign.event_id,
        EventLead.sponsor_answer.in_(["yes", "maybe"]),
        EventLead.automation_status == "active",
        EventLead.delivery_state == "not_started",
    )
    if body.lead_ids:
        statement = statement.where(EventLead.id.in_(body.lead_ids))
    simulation_start = body.now or datetime.now(UTC).replace(hour=10, minute=0, second=0)
    launched: list[str] = []
    for lead in session.scalars(statement).all():
        try:
            with session.begin_nested():
                start_lead_workflow(session, lead, campaign, simulation_start, settings)
            launched.append(lead.id)
        except ValueError:
            continue
    cycles = []
    for day in [0, 2, 5, 10]:
        logical_now = simulation_start + timedelta(days=day, minutes=10)
        cycles.append(
            {
                "logical_now": logical_now.isoformat(),
                "result": await run_worker_cycle(session, registry, settings, logical_now, 1000),
            }
        )
    audit(
        session,
        "campaign.simulate",
        "campaign",
        campaign.id,
        actor[0],
        {"launched": len(launched)},
    )
    session.commit()
    return {"campaign_id": campaign.id, "launched": launched, "cycles": cycles}


@app.post(f"{settings.api_prefix}/leads/{{lead_id}}/workflow/start")
async def start_workflow(
    lead_id: str,
    body: WorkflowStart,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    await require_live_provider_readiness(session)
    lead = get_or_404(session, EventLead, lead_id)
    campaign = get_or_404(session, Campaign, body.campaign_id)
    try:
        actions = start_lead_workflow(
            session, lead, campaign, body.now or datetime.now(UTC), registry.settings
        )
    except ValueError as exc:
        raise as_http_error(exc) from exc
    audit(session, "workflow.start", "lead", lead.id, actor[0])
    session.commit()
    return {"lead_id": lead.id, "scheduled_action_ids": [item.id for item in actions]}


@app.post(f"{settings.api_prefix}/worker/run-due")
async def run_due(
    body: WorkflowRunDue,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(require_write),
):
    await registry.refresh(session)
    return await run_worker_cycle(
        session, registry, registry.settings, body.now or datetime.now(UTC), body.limit
    )


@app.post("/webhooks/ses/events")
async def ses_events_webhook(
    request: Request,
    session: Session = Depends(get_session),
):
    await registry.refresh(session)
    try:
        envelope = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "request body is not a valid SNS envelope") from exc
    if not isinstance(envelope, dict):
        raise HTTPException(422, "SNS envelope must be a JSON object")
    try:
        await verify_sns_signature(envelope)
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    configured_topic = registry.settings.ses_sns_topic_arn
    if not configured_topic:
        raise HTTPException(503, "SES SNS topic pin is not configured")
    if not secrets.compare_digest(str(envelope.get("TopicArn") or ""), configured_topic):
        raise HTTPException(401, "unexpected SES SNS topic")
    if envelope.get("Type") == "SubscriptionConfirmation":
        try:
            await confirm_sns_subscription(envelope)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"accepted": True, "subscription_confirmed": True}
    if envelope.get("Type") != "Notification":
        return {"accepted": True, "ignored": envelope.get("Type")}
    try:
        event = json.loads(envelope["Message"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "SNS notification does not contain an SES event") from exc
    event_type = str(event.get("eventType") or event.get("notificationType") or "").lower()
    if event_type == "received":
        try:
            inbound = parse_ses_received_email(event)
            if not inbound["provider_event_id"]:
                raise ValueError("SES inbound email has no provider message ID")
            return await handle_inbound_event(
                session,
                registry,
                provider="ses",
                provider_event_id=str(inbound["provider_event_id"]),
                channel="email",
                identity=str(inbound["identity"]),
                lead_id=str(inbound["lead_id"]) if inbound["lead_id"] else None,
                body=str(inbound["body"]),
            )
        except ValueError as exc:
            if "envelope sender does not match" in str(exc):
                return {"accepted": True, "quarantined": True, "reason": str(exc)}
            raise as_http_error(exc) from exc
    status_map = {
        "send": "accepted",
        "delivery": "delivered",
        "bounce": "bounced",
        "complaint": "complained",
        "reject": "rejected",
        "rendering failure": "failed",
        "deliverydelay": "delayed",
    }
    status = status_map.get(event_type)
    mail = event.get("mail") or {}
    provider_message_id = mail.get("messageId")
    if not status or not provider_message_id:
        return {"accepted": True, "ignored": event_type}
    details = {
        "event_type": event_type,
        "timestamp": mail.get("timestamp"),
        "diagnostic": (event.get("bounce") or {}).get("bounceType")
        or (event.get("complaint") or {}).get("complaintFeedbackType")
        or (event.get("reject") or {}).get("reason")
        or (event.get("deliveryDelay") or {}).get("delayType"),
    }
    try:
        occurred_at = None
        provider_section = (
            event.get("delivery")
            or event.get("bounce")
            or event.get("complaint")
            or event.get("reject")
            or event.get("deliveryDelay")
            or {}
        )
        timestamp = provider_section.get("timestamp") or mail.get("timestamp")
        if timestamp:
            try:
                occurred_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            except ValueError:
                occurred_at = None
        tags = mail.get("tags") or {}
        tagged_key = tags.get("sponsorflow_id")
        if isinstance(tagged_key, list):
            tagged_key = tagged_key[0] if tagged_key else None
        return handle_delivery_event(
            session,
            provider="ses",
            provider_event_id=str(envelope["MessageId"]),
            provider_message_id=str(provider_message_id),
            status=status,
            occurred_at=occurred_at,
            details=details,
            idempotency_key=str(tagged_key) if tagged_key else None,
        )
    except ValueError as exc:
        raise as_http_error(exc) from exc


@app.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(
    request: Request,
    session: Session = Depends(get_session),
):
    await registry.refresh(session)
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected = registry.settings.whatsapp_verify_token
    if mode != "subscribe" or not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(403, "WhatsApp webhook verification failed")
    return Response(content=challenge or "", media_type="text/plain")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    session: Session = Depends(get_session),
    x_hub_signature_256: Annotated[str | None, Header()] = None,
):
    await registry.refresh(session)
    app_secret = registry.settings.whatsapp_app_secret
    if not app_secret or not x_hub_signature_256:
        raise HTTPException(401, "WhatsApp webhook signature is not configured")
    raw = await request.body()
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    supplied = x_hub_signature_256.removeprefix("sha256=")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid WhatsApp webhook signature")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "WhatsApp webhook body is not valid JSON") from exc
    processed: list[dict] = []
    errors: list[str] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                interactive = message.get("interactive") or {}
                body = (
                    (message.get("text") or {}).get("body")
                    or (interactive.get("button_reply") or {}).get("title")
                    or (interactive.get("list_reply") or {}).get("title")
                    or (message.get("image") or {}).get("caption")
                    or (message.get("document") or {}).get("caption")
                )
                if not body:
                    continue
                try:
                    processed.append(
                        await handle_inbound_event(
                            session,
                            registry,
                            provider="whatsapp",
                            provider_event_id=str(message["id"]),
                            channel="whatsapp",
                            identity=str(message["from"]),
                            body=str(body),
                            occurred_at=datetime.fromtimestamp(
                                int(message.get("timestamp", time.time())), UTC
                            ),
                        )
                    )
                except ValueError as exc:
                    session.rollback()
                    errors.append(str(exc))
            status_map = {
                "sent": "accepted",
                "delivered": "delivered",
                "read": "read",
                "failed": "failed",
            }
            for status in value.get("statuses", []):
                normalized = status_map.get(status.get("status"))
                if not normalized:
                    continue
                try:
                    processed.append(
                        handle_delivery_event(
                            session,
                            provider="whatsapp",
                            provider_event_id=(
                                f"{status.get('id')}:{normalized}:{status.get('timestamp')}"
                            ),
                            provider_message_id=str(status["id"]),
                            status=normalized,
                            occurred_at=datetime.fromtimestamp(
                                int(status.get("timestamp", time.time())), UTC
                            ),
                            details=status.get("errors") or {},
                        )
                    )
                except ValueError as exc:
                    session.rollback()
                    errors.append(str(exc))
    return {"accepted": True, "processed": len(processed), "errors": errors}


@app.post("/webhooks/calcom")
async def calcom_webhook(
    request: Request,
    session: Session = Depends(get_session),
    x_cal_signature_256: Annotated[str | None, Header()] = None,
):
    await registry.refresh(session)
    secret = registry.settings.calcom_webhook_secret
    if not secret or not x_cal_signature_256:
        raise HTTPException(401, "Cal.com webhook signature is not configured")
    raw = await request.body()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(x_cal_signature_256.removeprefix("sha256="), expected):
        raise HTTPException(401, "invalid Cal.com webhook signature")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "Cal.com webhook body is not valid JSON") from exc
    trigger = str(body.get("triggerEvent") or body.get("type") or "").upper()
    payload = body.get("payload") or body.get("data") or {}
    booking_id = payload.get("uid") or payload.get("id")
    if not booking_id:
        raise HTTPException(422, "Cal.com webhook has no booking ID")
    status_map = {
        "BOOKING_CREATED": "booked",
        "BOOKING_CONFIRMED": "booked",
        "BOOKING_RESCHEDULED": "rescheduled",
        "BOOKING_CANCELLED": "cancelled",
        "BOOKING_REJECTED": "rejected",
        "BOOKING_COMPLETED": "completed",
        "BOOKING_NO_SHOW": "no_show",
        "BOOKING_REOPENED": "booked",
    }
    status = status_map.get(trigger)
    if not status:
        return {"accepted": True, "ignored": trigger}
    start_value = payload.get("startTime") or payload.get("start")
    starts_at = (
        datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
        if start_value
        else None
    )
    event_id = str(body.get("id") or f"{trigger}:{booking_id}:{start_value or ''}")
    metadata = payload.get("metadata") or {}
    attendees = payload.get("attendees") or []
    attendee = attendees[0] if attendees and isinstance(attendees[0], dict) else {}
    try:
        return handle_calendar_event(
            session,
            provider="calcom",
            provider_event_id=event_id,
            provider_booking_id=str(booking_id),
            status=status,
            starts_at=starts_at,
            details={
                "trigger": trigger,
                "previous_booking_id": payload.get("rescheduledFromUid")
                or payload.get("rescheduleUid"),
            },
            lead_id=metadata.get("sponsorflowLeadId"),
            timezone=str(attendee.get("timeZone") or payload.get("timeZone") or "UTC"),
            booking_url=payload.get("meetingUrl") or payload.get("bookingUrl"),
        )
    except ValueError as exc:
        raise as_http_error(exc) from exc


@app.post(f"{settings.api_prefix}/inbound")
async def inbound(
    body: InboundEventRequest,
    session: Session = Depends(get_session),
    _verified: None = Depends(require_webhook_signature),
):
    try:
        return await handle_inbound_event(
            session,
            registry,
            provider=body.provider,
            provider_event_id=body.provider_event_id,
            channel=body.channel,
            identity=body.identity,
            lead_id=body.lead_id,
            body=body.body,
            occurred_at=body.occurred_at,
        )
    except ValueError as exc:
        raise as_http_error(exc) from exc


@app.post(f"{settings.api_prefix}/inbound/delivery")
def inbound_delivery(
    body: DeliveryEventRequest,
    session: Session = Depends(get_session),
    _verified: None = Depends(require_webhook_signature),
):
    try:
        return handle_delivery_event(
            session,
            provider=body.provider,
            provider_event_id=body.provider_event_id,
            provider_message_id=body.provider_message_id,
            status=body.status,
            occurred_at=body.occurred_at,
            details=body.details,
        )
    except ValueError as exc:
        raise as_http_error(exc) from exc


@app.post(f"{settings.api_prefix}/leads/{{lead_id}}/offers", status_code=201)
def propose_offer(
    lead_id: str,
    body: OfferRequest,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    lead = get_or_404(session, EventLead, lead_id)
    try:
        offer = create_offer(
            session, lead, body.package_id, body.offered_price, body.perks, body.rationale
        )
    except ValueError as exc:
        raise as_http_error(exc) from exc
    audit(session, "offer.create", "offer", offer.id, actor[0])
    action = queue_offer_message(session, lead, offer) if body.send_immediately else None
    session.commit()
    return {
        "id": offer.id,
        "package_id": offer.package_id,
        "offered_price": str(offer.offered_price),
        "discount_percent": str(offer.discount_percent),
        "status": offer.status,
        "scheduled_action_id": action.id if action else None,
    }


@app.post(f"{settings.api_prefix}/leads/{{lead_id}}/meetings", status_code=201)
async def create_meeting(
    lead_id: str,
    body: MeetingRequest,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    lead = get_or_404(session, EventLead, lead_id)
    meeting = await book_meeting(
        session, registry, lead, body.starts_at, body.timezone
    )
    audit(session, "meeting.book", "meeting", meeting.id, actor[0])
    session.commit()
    return {
        "id": meeting.id,
        "provider_booking_id": meeting.provider_booking_id,
        "starts_at": meeting.starts_at,
        "timezone": meeting.timezone,
        "booking_url": meeting.booking_url,
    }


@app.post(f"{settings.api_prefix}/leads/{{lead_id}}/manual-reply")
def manual_reply(
    lead_id: str,
    body: ManualReplyRequest,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_write),
):
    lead = get_or_404(session, EventLead, lead_id)
    action = queue_manual_reply(session, lead, body.channel, body.body, actor[0])
    return {"action_id": action.id, "status": action.status, "automation_status": "manual"}


@app.get(f"{settings.api_prefix}/operations/suppressions")
def list_suppressions(
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    entries = session.scalars(
        select(SuppressionEntry).order_by(SuppressionEntry.created_at.desc())
    ).all()
    return [
        {
            "id": entry.id,
            "contact_id": entry.contact_id,
            "identity_type": entry.identity_type,
            "identity_value": entry.identity_value,
            "scope": entry.scope,
            "reason": entry.reason,
            "source": entry.source,
            "created_at": entry.created_at,
        }
        for entry in entries
    ]


@app.delete(f"{settings.api_prefix}/operations/suppressions/contact/{{contact_id}}")
def remove_contact_suppression(
    contact_id: str,
    session: Session = Depends(get_session),
    actor: tuple[str, str] = Depends(require_admin),
):
    contact = get_or_404(session, Contact, contact_id)
    entries = session.scalars(
        select(SuppressionEntry).where(SuppressionEntry.contact_id == contact.id)
    ).all()
    for entry in entries:
        session.delete(entry)
    audit(
        session,
        "contact.unsuppress",
        "contact",
        contact.id,
        actor[0],
        {"removed_identities": len(entries)},
    )
    session.commit()
    return {
        "contact_id": contact.id,
        "removed_identities": len(entries),
        "note": "lead automation remains stopped until an operator explicitly reviews and resumes it",
    }


@app.get(f"{settings.api_prefix}/operations/audit")
def list_audit_events(
    limit: int = 100,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    safe_limit = max(1, min(limit, 500))
    events = session.scalars(
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(safe_limit)
    ).all()
    return [
        {
            "id": event.id,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "data": event.data,
            "created_at": event.created_at,
        }
        for event in events
    ]


@app.get(f"{settings.api_prefix}/operations/actions")
def list_scheduled_actions(
    status: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    statement = select(ScheduledAction).order_by(ScheduledAction.due_at).limit(
        max(1, min(limit, 500))
    )
    if status:
        statement = statement.where(ScheduledAction.status == status)
    actions = session.scalars(statement).all()
    return [
        {
            "id": action.id,
            "lead_id": action.lead_id,
            "type": action.action_type,
            "channel": action.channel,
            "due_at": action.due_at,
            "status": action.status,
            "cancelled_reason": action.cancelled_reason,
        }
        for action in actions
    ]


@app.get(f"{settings.api_prefix}/analytics/overview")
def analytics(
    event_id: str | None = None,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    lead_query = select(EventLead.state, func.count(EventLead.id)).group_by(EventLead.state)
    if event_id:
        lead_query = lead_query.where(EventLead.event_id == event_id)
    pipeline = {state: count for state, count in session.execute(lead_query).all()}
    message_query = select(
        Message.direction, Message.channel, func.count(Message.id)
    ).group_by(Message.direction, Message.channel)
    if event_id:
        message_query = (
            message_query.join(Conversation, Conversation.id == Message.conversation_id)
            .join(EventLead, EventLead.id == Conversation.lead_id)
            .where(EventLead.event_id == event_id)
        )
    messages = {
        f"{direction}:{channel}": count
        for direction, channel, count in session.execute(message_query).all()
    }
    try:
        quota_zone = ZoneInfo(settings.telegram_quota_timezone)
    except ZoneInfoNotFoundError:
        quota_zone = ZoneInfo("UTC")
    quota_date = datetime.now(UTC).astimezone(quota_zone).date()
    today_quota = session.get(TelegramDailyQuota, quota_date)
    total_leads = sum(pipeline.values())
    engaged = sum(
        pipeline.get(state, 0)
        for state in ["engaged", "qualified", "negotiating", "call_booked", "won"]
    )
    return {
        "pipeline": pipeline,
        "messages": messages,
        "rates": {
            "engaged_or_better": engaged,
            "total_leads": total_leads,
            "engagement_percent": round(engaged / total_leads * 100, 1) if total_leads else 0,
        },
        "telegram_quota": {
            "date": str(quota_date),
            "reserved": today_quota.reserved_count if today_quota else 0,
            "limit": today_quota.limit_count
            if today_quota
            else settings.telegram_daily_new_contact_limit,
        },
        "pending_actions": session.scalar(
            select(func.count(ScheduledAction.id)).where(ScheduledAction.status == "pending")
        ),
        "pending_outbox": session.scalar(
            select(func.count(OutboxEvent.id)).where(OutboxEvent.status == "pending")
        ),
        "suppressed_identities": session.scalar(select(func.count(SuppressionEntry.id))),
    }


@app.get(f"{settings.api_prefix}/events/{{event_id}}/export.csv")
def export_event(
    event_id: str,
    session: Session = Depends(get_session),
    _actor: tuple[str, str] = Depends(actor_context),
):
    get_or_404(session, Event, event_id)
    rows = session.execute(
        select(EventLead, Contact)
        .join(Contact, Contact.id == EventLead.contact_id)
        .where(EventLead.event_id == event_id)
    ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["lead_id", "name", "email", "telegram", "whatsapp", "sponsor_answer", "state"]
    )
    for lead, contact in rows:
        writer.writerow(
            [
                lead.id,
                contact.full_name,
                contact.email_normalized,
                contact.telegram_normalized,
                contact.whatsapp_normalized or "",
                lead.sponsor_answer,
                lead.state,
            ]
        )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{event_id}-leads.csv"'},
    )
