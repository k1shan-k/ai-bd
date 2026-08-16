import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ProviderConfig, utcnow


@dataclass(frozen=True)
class ProviderDefinition:
    label: str
    config_fields: dict[str, dict[str, Any]]
    secret_fields: tuple[str, ...]


PROVIDERS: dict[str, ProviderDefinition] = {
    "ses": ProviderDefinition(
        "Amazon SES",
        {
            "region": {"label": "AWS region", "placeholder": "us-east-1", "required": True},
            "sender_email": {"label": "Verified sender email", "type": "email", "required": True},
            "sender_name": {"label": "Sender name", "placeholder": "Sponsorship Team"},
            "reply_to": {"label": "Reply-to email (plus addressing required for exact reply correlation)", "type": "email", "required": True},
            "configuration_set": {"label": "Configuration set", "required": True},
            "sns_topic_arn": {"label": "SES event and receipt SNS topic ARN", "required": True},
            "subject": {"label": "Default subject"},
        },
        ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"),
    ),
    "telegram": ProviderDefinition(
        "Telegram personal account",
        {
            "api_id": {"label": "Telegram API ID", "type": "number", "required": True},
            "phone": {"label": "Account phone number", "placeholder": "+15551234567", "required": True},
        },
        ("api_hash", "session_string"),
    ),
    "whatsapp": ProviderDefinition(
        "WhatsApp Business Cloud",
        {
            "phone_number_id": {"label": "Phone number ID", "required": True},
            "graph_version": {"label": "Graph API version", "placeholder": "v23.0", "required": True},
            "template_name": {"label": "Fallback template name", "required": True},
            "template_language": {"label": "Template language", "placeholder": "en_US"},
            "template_body_mode": {
                "label": "Template body parameters",
                "type": "select",
                "options": ["message_body", "none"],
            },
        },
        ("access_token", "app_secret", "verify_token"),
    ),
    "calcom": ProviderDefinition(
        "Cal.com",
        {
            "event_type_id": {"label": "Event type ID", "type": "number", "required": True},
            "base_url": {"label": "API base URL", "placeholder": "https://api.cal.com/v2"},
            "api_version": {"label": "API version", "placeholder": "2024-08-13"},
        },
        ("api_key", "webhook_secret"),
    ),
    "tavily": ProviderDefinition(
        "Tavily web research",
        {
            "result_limit": {"label": "Results per lead", "type": "number", "placeholder": "5"},
            "search_depth": {"label": "Search depth", "type": "select", "options": ["basic", "advanced"]},
            "base_url": {"label": "API base URL", "placeholder": "https://api.tavily.com"},
        },
        ("api_key",),
    ),
}


def _key(settings: Settings) -> bytes:
    raw = settings.provider_encryption_key
    if not raw:
        raise ValueError("provider encryption key is not configured on the server")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise ValueError("provider encryption key is not valid base64") from exc
    if len(key) != 32:
        raise ValueError("provider encryption key must decode to exactly 32 bytes")
    return key


def encrypt_secrets(settings: Settings, provider: str, secrets: dict[str, str]) -> tuple[str, str]:
    nonce = os.urandom(12)
    plaintext = json.dumps(secrets, sort_keys=True, separators=(",", ":")).encode()
    ciphertext = AESGCM(_key(settings)).encrypt(nonce, plaintext, f"{provider}:v1".encode())
    return (
        base64.urlsafe_b64encode(ciphertext).decode(),
        base64.urlsafe_b64encode(nonce).decode(),
    )


def decrypt_secrets(settings: Settings, row: ProviderConfig) -> dict[str, str]:
    if not row.encrypted_secrets:
        return {}
    try:
        ciphertext = base64.urlsafe_b64decode(row.encrypted_secrets)
        nonce = base64.urlsafe_b64decode(row.nonce)
        plaintext = AESGCM(_key(settings)).decrypt(
            nonce, ciphertext, f"{row.provider}:v{row.key_version}".encode()
        )
        value = json.loads(plaintext)
    except Exception as exc:
        raise ValueError(f"cannot decrypt {row.provider} provider credentials") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid encrypted credential payload for {row.provider}")
    return {str(key): str(item) for key, item in value.items()}


def provider_rows(session: Session) -> list[ProviderConfig]:
    return session.scalars(select(ProviderConfig).order_by(ProviderConfig.provider)).all()


def _field_descriptors(definition: ProviderDefinition) -> list[dict[str, Any]]:
    return [{"name": name, **details} for name, details in definition.config_fields.items()]


def serialize_provider(settings: Settings, row: ProviderConfig | None, provider: str) -> dict[str, Any]:
    definition = PROVIDERS[provider]
    secrets = decrypt_secrets(settings, row) if row else {}
    return {
        "provider": provider,
        "label": definition.label,
        "enabled": bool(row and row.enabled),
        "revision": row.revision if row else 0,
        "config": row.config if row else {},
        "secret_fields": {name: bool(secrets.get(name)) for name in definition.secret_fields},
        "config_fields": _field_descriptors(definition),
        "last_check_status": row.last_check_status if row else None,
        "last_check_details": row.last_check_details if row else {},
        "last_checked_at": row.last_checked_at if row else None,
    }


def list_provider_configs(session: Session, settings: Settings) -> list[dict[str, Any]]:
    rows = {row.provider: row for row in provider_rows(session)}
    return [serialize_provider(settings, rows.get(provider), provider) for provider in PROVIDERS]


def update_provider_config(
    session: Session,
    settings: Settings,
    provider: str,
    *,
    enabled: bool,
    config: dict[str, Any],
    supplied_secrets: dict[str, str],
    clear_secrets: list[str],
    expected_revision: int | None,
    actor: str,
) -> ProviderConfig:
    definition = PROVIDERS.get(provider)
    if not definition:
        raise ValueError("unknown provider")
    unknown_config = set(config) - set(definition.config_fields)
    unknown_secrets = set(supplied_secrets) - set(definition.secret_fields)
    unknown_clear = set(clear_secrets) - set(definition.secret_fields)
    if unknown_config or unknown_secrets or unknown_clear:
        raise ValueError("provider configuration contains unknown fields")
    for name, descriptor in definition.config_fields.items():
        if enabled and descriptor.get("required") and not str(config.get(name) or "").strip():
            raise ValueError(f"{descriptor['label']} is required before enabling {provider}")
    for name in ("api_id", "event_type_id", "result_limit"):
        if name in config and str(config.get(name) or "").strip():
            try:
                if int(config[name]) <= 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a positive integer") from exc
    allowed_hosts = {
        "calcom": ("api.cal.com",),
        "tavily": ("api.tavily.com",),
    }
    if provider in allowed_hosts and config.get("base_url"):
        parsed = urlparse(str(config["base_url"]))
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts[provider]:
            raise ValueError(f"{provider} API base URL must use the official HTTPS host")
    row = session.scalar(
        select(ProviderConfig).where(ProviderConfig.provider == provider).with_for_update()
    )
    if row and expected_revision is None:
        raise ValueError("expected_revision is required when updating provider configuration")
    if row and row.revision != expected_revision:
        raise ValueError("provider configuration changed; reload before saving")
    current_secrets = decrypt_secrets(settings, row) if row else {}
    for name in clear_secrets:
        current_secrets.pop(name, None)
    current_secrets.update(
        {name: value for name, value in supplied_secrets.items() if value.strip()}
    )
    required_secrets = {
        "telegram": {"api_hash", "session_string"},
        "whatsapp": {"access_token", "app_secret", "verify_token"},
        "calcom": {"api_key", "webhook_secret"},
        "tavily": {"api_key"},
    }
    missing_secrets = sorted(
        name for name in required_secrets.get(provider, set()) if not current_secrets.get(name)
    )
    if enabled and missing_secrets:
        raise ValueError(
            f"{provider} credentials are missing before enablement: " + ", ".join(missing_secrets)
        )
    encrypted, nonce = encrypt_secrets(settings, provider, current_secrets)
    if not row:
        row = ProviderConfig(provider=provider)
        session.add(row)
    else:
        row.revision += 1
    row.enabled = enabled
    row.config = config
    row.encrypted_secrets = encrypted
    row.nonce = nonce
    row.key_version = 1
    row.updated_by = actor
    row.last_check_status = None
    row.last_check_details = {}
    row.last_checked_at = None
    session.flush()
    return row


def write_provider_secrets(
    settings: Settings, row: ProviderConfig, secrets: dict[str, str]
) -> None:
    encrypted, nonce = encrypt_secrets(settings, row.provider, secrets)
    row.encrypted_secrets = encrypted
    row.nonce = nonce
    row.key_version = 1


def _redact_check_details(value: Any, key: str = "") -> Any:
    sensitive = ("token", "secret", "password", "authorization", "api_key", "session")
    if any(term in key.casefold() for term in sensitive):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name): _redact_check_details(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_check_details(item, key) for item in value]
    return value


def mark_provider_check(
    row: ProviderConfig, *, ok: bool, details: dict[str, Any]
) -> None:
    row.last_check_status = "ready" if ok else "failed"
    row.last_check_details = _redact_check_details(details)
    row.last_checked_at = utcnow()


def settings_from_store(session: Session, base: Settings) -> tuple[Settings, str]:
    rows = provider_rows(session)
    if not rows:
        return base, "environment"
    updates: dict[str, Any] = {}
    revisions: list[str] = []
    for row in rows:
        revisions.append(f"{row.provider}:{row.revision}:{int(row.enabled)}")
        config = row.config or {}
        secrets = decrypt_secrets(base, row)
        if row.provider == "ses":
            updates.update(
                {
                    "ses_region": config.get("region") if row.enabled else None,
                    "ses_sender_email": config.get("sender_email") if row.enabled else None,
                    "ses_sender_name": config.get("sender_name") or "Sponsorship Team",
                    "ses_reply_to": config.get("reply_to"),
                    "ses_configuration_set": config.get("configuration_set"),
                    "ses_sns_topic_arn": config.get("sns_topic_arn"),
                    "ses_subject": config.get("subject") or "Sponsorship opportunity",
                    "aws_access_key_id": secrets.get("aws_access_key_id") if row.enabled else None,
                    "aws_secret_access_key": secrets.get("aws_secret_access_key") if row.enabled else None,
                    "aws_session_token": secrets.get("aws_session_token") if row.enabled else None,
                }
            )
        elif row.provider == "telegram":
            updates.update(
                {
                    "telegram_api_id": int(config["api_id"]) if row.enabled and config.get("api_id") else None,
                    "telegram_api_hash": secrets.get("api_hash") if row.enabled else None,
                    "telegram_session_string": secrets.get("session_string") if row.enabled else None,
                }
            )
        elif row.provider == "whatsapp":
            updates.update(
                {
                    "whatsapp_phone_number_id": config.get("phone_number_id") if row.enabled else None,
                    "whatsapp_graph_version": config.get("graph_version") if row.enabled else None,
                    "whatsapp_template_name": config.get("template_name") if row.enabled else None,
                    "whatsapp_template_language": config.get("template_language") or "en_US",
                    "whatsapp_template_body_mode": config.get("template_body_mode") or "message_body",
                    "whatsapp_access_token": secrets.get("access_token") if row.enabled else None,
                    "whatsapp_app_secret": secrets.get("app_secret") if row.enabled else None,
                    "whatsapp_verify_token": secrets.get("verify_token") if row.enabled else None,
                }
            )
        elif row.provider == "calcom":
            updates.update(
                {
                    "calcom_event_type_id": int(config["event_type_id"]) if row.enabled and config.get("event_type_id") else None,
                    "calcom_base_url": config.get("base_url") or "https://api.cal.com/v2",
                    "calcom_api_version": config.get("api_version") or "2024-08-13",
                    "calcom_api_key": secrets.get("api_key") if row.enabled else None,
                    "calcom_webhook_secret": secrets.get("webhook_secret") if row.enabled else None,
                }
            )
        elif row.provider == "tavily":
            updates.update(
                {
                    "research_provider": "tavily" if row.enabled else "fake",
                    "tavily_result_limit": int(config.get("result_limit") or 5),
                    "tavily_search_depth": config.get("search_depth") or "advanced",
                    "tavily_base_url": config.get("base_url") or "https://api.tavily.com",
                    "tavily_api_key": secrets.get("api_key") if row.enabled else None,
                }
            )
    fingerprint = hashlib.sha256("|".join(sorted(revisions)).encode()).hexdigest()
    return base.model_copy(update=updates), fingerprint
