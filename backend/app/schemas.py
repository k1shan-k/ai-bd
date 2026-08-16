from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EventCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$")
    name: str = Field(min_length=2, max_length=255)
    starts_at: datetime | None = None
    outreach_cutoff_at: datetime | None = None
    timezone: str = "UTC"


class EventRead(ORMModel):
    id: str
    slug: str
    name: str
    starts_at: datetime | None
    outreach_cutoff_at: datetime | None
    timezone: str
    status: str


class ContextActivate(BaseModel):
    documents: dict[str, str]
    created_by: str = "operator"


class ContextRead(ORMModel):
    id: str
    event_id: str
    version: int
    content_hash: str
    compiled: dict[str, Any]
    activated_at: datetime


class CampaignCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    context_version_id: str
    followup_days: list[int] = [2, 5, 10]
    whatsapp_fallback_day: int = 5


class CampaignRead(ORMModel):
    id: str
    event_id: str
    context_version_id: str
    name: str
    status: str
    followup_days: list[int]
    whatsapp_fallback_day: int


class ImportMapping(BaseModel):
    full_name: str = "name"
    email: str = "email"
    telegram: str = "telegram"
    whatsapp: str | None = "whatsapp"
    company: str | None = "company"
    role: str | None = "role"
    timezone: str | None = "timezone"
    sponsor_answer: str = "sponsor_answer"


class ImportPreview(BaseModel):
    headers: list[str]
    sample: list[dict[str, str]]
    detected_mapping: ImportMapping
    file_hash: str


class ImportSummary(BaseModel):
    import_job_id: str
    eligible: int = 0
    ineligible: int = 0
    duplicate: int = 0
    suppressed: int = 0
    invalid: int = 0
    quarantined: int = 0


class LeadRead(BaseModel):
    id: str
    full_name: str
    email: str
    telegram: str
    whatsapp: str | None
    company: str | None
    role: str | None
    sponsor_answer: str
    state: str
    delivery_state: str
    automation_status: str
    created_at: datetime


class LeadUpdate(BaseModel):
    state: str | None = None
    accepted_offer_id: str | None = None
    automation_status: Literal["active", "paused", "manual", "stopped"] | None = None
    note: str | None = None


class SuppressRequest(BaseModel):
    reason: str = "manual_block"
    actor: str = "operator"


class ResearchRequest(BaseModel):
    provider: str | None = None


class ResearchRead(ORMModel):
    id: str
    lead_id: str
    provider: str
    summary: str
    facts: list[Any]
    fit_angles: list[str]
    confidence: Decimal
    created_at: datetime


class CampaignLaunch(BaseModel):
    now: datetime | None = None
    lead_ids: list[str] | None = None


class WorkflowStart(BaseModel):
    campaign_id: str
    now: datetime | None = None


class WorkflowRunDue(BaseModel):
    now: datetime | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class InboundEventRequest(BaseModel):
    provider: Literal["ses", "telegram", "whatsapp", "fake"]
    provider_event_id: str
    channel: Literal["email", "telegram", "whatsapp"]
    identity: str
    lead_id: str | None = None
    body: str
    occurred_at: datetime | None = None


class DeliveryEventRequest(BaseModel):
    provider: Literal["ses", "telegram", "whatsapp", "fake"]
    provider_event_id: str
    provider_message_id: str
    status: Literal[
        "accepted", "delayed", "delivered", "read", "failed", "bounced", "complained", "rejected"
    ]
    occurred_at: datetime | None = None
    details: dict[str, Any] = {}


class OfferRequest(BaseModel):
    package_id: str
    offered_price: Decimal
    perks: list[str] = []
    rationale: str = ""
    send_immediately: bool = True


class MeetingRequest(BaseModel):
    starts_at: datetime
    timezone: str = "UTC"


class ManualReplyRequest(BaseModel):
    channel: Literal["email", "telegram", "whatsapp"]
    body: str = Field(min_length=1)
    actor: str = "operator"


class TelegramAuthConfirm(BaseModel):
    code: str = Field(min_length=3, max_length=20)
    password: str | None = None


class ProviderConfigUpdate(BaseModel):
    enabled: bool = False
    config: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    clear_secrets: list[str] = []
    expected_revision: int | None = None


class ProviderConfigRead(BaseModel):
    provider: str
    label: str
    enabled: bool
    revision: int
    config: dict[str, Any]
    secret_fields: dict[str, bool]
    config_fields: list[dict[str, Any]]
    last_check_status: str | None = None
    last_check_details: dict[str, Any] = {}
    last_checked_at: datetime | None = None


class ProviderCheckRead(BaseModel):
    provider: str
    configured: bool
    mode: str
    details: dict[str, Any]
