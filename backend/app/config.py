from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SPONSORFLOW_", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./sponsorflow.db"
    storage_path: Path = Path("./data")
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000"]
    outreach_start_hour: int = Field(default=9, ge=0, le=23)
    outreach_end_hour: int = Field(default=18, ge=1, le=24)
    outreach_jitter_min_minutes: int = Field(default=4, ge=0, le=120)
    outreach_jitter_max_minutes: int = Field(default=28, ge=0, le=240)
    cross_channel_gap_min_minutes: int = Field(default=45, ge=1, le=360)
    cross_channel_gap_max_minutes: int = Field(default=150, ge=1, le=720)
    conversation_reply_min_seconds: int = Field(default=35, ge=0, le=600)
    conversation_reply_max_seconds: int = Field(default=600, ge=1, le=3600)
    telegram_daily_new_contact_limit: int = Field(default=20, ge=1, le=20)
    telegram_quota_timezone: str = "UTC"
    minimum_research_confidence: float = Field(default=0.6, ge=0, le=1)
    provider_mode: Literal["fake", "live"] = "fake"
    admin_api_key: str | None = None
    operator_api_key: str | None = None
    viewer_api_key: str | None = None
    inbound_webhook_token: str | None = None

    # AI brain. Fake is deterministic test-only behavior; production requires a real provider.
    llm_provider: Literal["fake", "gateway", "bedrock", "vertex_maas", "nvidia_nim", "disabled"] = (
        "fake"
    )
    llm_model: str = ""
    llm_gateway_url: str | None = None
    llm_gateway_path: str = "/v1/chat/completions"
    llm_gateway_protocol: Literal["openai", "simple"] = "openai"
    llm_api_key: str | None = None
    llm_bedrock_region: str = "us-east-1"
    llm_vertex_project_id: str | None = Field(default=None, min_length=1, max_length=128)
    llm_vertex_region: str = Field(
        default="us-east5", pattern=r"^[a-z0-9-]+$", min_length=1, max_length=63
    )
    llm_vertex_endpoint: str | None = None
    llm_vertex_api_version: Literal["v1", "v1beta1"] = "v1beta1"
    llm_vertex_auth_mode: Literal["gcloud", "adc", "metadata", "access_token"] = "metadata"
    llm_vertex_access_token: str | None = None
    llm_vertex_token_ttl_seconds: int = Field(default=3_000, ge=60, le=3_500)
    llm_nvidia_endpoint: Literal[
        "https://integrate.api.nvidia.com/v1/chat/completions"
    ] = "https://integrate.api.nvidia.com/v1/chat/completions"
    llm_nvidia_api_key: str | None = None
    llm_nvidia_top_p: float = Field(default=0.95, ge=0, le=1)
    llm_nvidia_thinking: bool = True
    llm_nvidia_reasoning_effort: Literal["low", "medium", "high"] = "high"
    llm_timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_input_chars: int = Field(default=60_000, ge=1_000, le=250_000)
    llm_max_output_tokens: int = Field(default=1_200, ge=128, le=16_384)
    llm_outreach_temperature: float = Field(default=0.45, ge=0, le=1)
    llm_reply_temperature: float = Field(default=0.25, ge=0, le=1)
    llm_min_confidence: float = Field(default=0.72, ge=0, le=1)
    llm_autonomy: Literal["draft", "guarded_auto", "manual"] = "guarded_auto"
    llm_generation_stale_seconds: int = Field(default=600, ge=30, le=3600)

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
        timing_ranges = {
            "outreach jitter": (
                self.outreach_jitter_min_minutes,
                self.outreach_jitter_max_minutes,
            ),
            "cross-channel gap": (
                self.cross_channel_gap_min_minutes,
                self.cross_channel_gap_max_minutes,
            ),
            "conversation reply delay": (
                self.conversation_reply_min_seconds,
                self.conversation_reply_max_seconds,
            ),
        }
        for label, (minimum, maximum) in timing_ranges.items():
            if minimum > maximum:
                raise ValueError(f"{label} minimum must not exceed maximum")
        if self.environment == "production":
            if self.provider_mode != "live":
                raise ValueError(
                    "production requires provider_mode=live; fake sends are simulation only"
                )
            if self.llm_provider not in {"gateway", "bedrock", "vertex_maas", "nvidia_nim"}:
                raise ValueError(
                    "production requires llm_provider=gateway, bedrock, vertex_maas, or nvidia_nim"
                )
            if not self.llm_model:
                raise ValueError("production requires an explicit llm_model")
            if self.llm_provider == "gateway" and not self.llm_gateway_url:
                raise ValueError("production gateway LLM requires llm_gateway_url")
            if self.llm_provider == "gateway" and not self.llm_api_key:
                raise ValueError("production gateway LLM requires llm_api_key")
            if self.llm_provider == "vertex_maas" and not self.llm_vertex_project_id:
                raise ValueError("production Vertex MaaS requires llm_vertex_project_id")
            if self.llm_provider == "vertex_maas" and self.llm_vertex_auth_mode != "metadata":
                raise ValueError(
                    "production Vertex MaaS requires metadata service-account authentication"
                )
            if self.llm_provider == "nvidia_nim" and not self.llm_nvidia_api_key:
                raise ValueError("production NVIDIA NIM requires llm_nvidia_api_key")
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
