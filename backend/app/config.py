from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SPONSORFLOW_", extra="ignore"
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./sponsorflow.db"
    storage_path: Path = Path("./data")
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000"]
    outreach_start_hour: int = Field(default=9, ge=0, le=23)
    outreach_end_hour: int = Field(default=18, ge=1, le=24)
    telegram_daily_new_contact_limit: int = Field(default=20, ge=1, le=20)
    telegram_quota_timezone: str = "UTC"
    minimum_research_confidence: float = Field(default=0.6, ge=0, le=1)
    provider_mode: Literal["fake", "live"] = "fake"
    admin_api_key: str | None = None
    operator_api_key: str | None = None
    viewer_api_key: str | None = None
    inbound_webhook_token: str | None = None

    # Provider account secrets are configured in the admin UI and encrypted with this key.
    provider_encryption_key: str | None = None

    # Amazon SES v2 uses the ambient AWS credential chain (task role preferred).
    ses_region: str | None = None
    ses_sender_email: str | None = None
    ses_sender_name: str = "Sponsorship Team"
    ses_reply_to: str | None = None
    ses_configuration_set: str | None = None
    ses_sns_topic_arn: str | None = None
    ses_subject: str = "Sponsorship opportunity"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    # Telegram personal-account MTProto credentials and an encrypted-at-rest StringSession.
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_session_string: str | None = None

    # Meta WhatsApp Business Cloud API.
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_graph_version: str | None = None
    whatsapp_template_name: str | None = None
    whatsapp_template_language: str = "en_US"
    whatsapp_template_body_mode: Literal["message_body", "none"] = "message_body"

    # Cal.com API v2.
    calendar_api_key: str | None = None  # Backward-compatible alias for calcom_api_key.
    calcom_api_key: str | None = None
    calcom_base_url: str = "https://api.cal.com/v2"
    calcom_api_version: str = "2024-08-13"
    calcom_event_type_id: int | None = None
    calcom_webhook_secret: str | None = None

    # Tavily web research.
    research_provider: Literal["fake", "tavily"] = "fake"
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"
    tavily_result_limit: int = Field(default=5, ge=1, le=10)
    tavily_search_depth: Literal["basic", "advanced"] = "advanced"

    @model_validator(mode="after")
    def validate_contact_window(self) -> "Settings":
        if self.outreach_start_hour >= self.outreach_end_hour:
            raise ValueError("outreach_start_hour must be before outreach_end_hour")
        if self.environment == "production":
            if self.provider_mode != "live":
                raise ValueError("production requires provider_mode=live; fake sends are simulation only")
            required = {
                "admin_api_key": self.admin_api_key,
                "inbound_webhook_token": self.inbound_webhook_token,
                "provider_encryption_key": self.provider_encryption_key,
            }
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError(
                    "production bootstrap configuration is missing: " + ", ".join(missing)
                )
        return self


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    return settings
