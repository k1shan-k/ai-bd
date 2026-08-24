import pytest
from app.config import Settings
from pydantic import ValidationError


def test_telegram_limit_is_a_hard_twenty():
    with pytest.raises(ValidationError):
        Settings(telegram_daily_new_contact_limit=21)
    assert Settings(telegram_daily_new_contact_limit=5).telegram_daily_new_contact_limit == 5


def test_production_cannot_record_fake_provider_sends():
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            provider_mode="fake",
            admin_api_key="admin-secret",
            inbound_webhook_token="webhook-secret",
        )


def production_vertex_settings(**updates):
    values = {
        "environment": "production",
        "provider_mode": "live",
        "llm_provider": "vertex_maas",
        "llm_model": "meta/llama-4-maverick-17b-128e-instruct-maas",
        "llm_vertex_project_id": "example-project",
        "llm_vertex_auth_mode": "metadata",
        "admin_api_key": "admin-secret",
        "inbound_webhook_token": "webhook-secret",
        "provider_encryption_key": "encryption-secret",
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


def test_production_accepts_vertex_metadata_service_account_auth():
    configured = production_vertex_settings()
    assert configured.llm_provider == "vertex_maas"
    assert configured.llm_vertex_auth_mode == "metadata"


@pytest.mark.parametrize("auth_mode", ["gcloud", "adc", "access_token"])
def test_production_vertex_rejects_non_metadata_auth(auth_mode):
    with pytest.raises(ValidationError, match="metadata service-account"):
        production_vertex_settings(
            llm_vertex_auth_mode=auth_mode,
            llm_vertex_access_token="test-token" if auth_mode == "access_token" else None,
        )


def test_production_vertex_requires_project_id():
    with pytest.raises(ValidationError, match="llm_vertex_project_id"):
        production_vertex_settings(llm_vertex_project_id=None)


def production_nvidia_settings(**updates):
    values = {
        "environment": "production",
        "provider_mode": "live",
        "llm_provider": "nvidia_nim",
        "llm_model": "deepseek-ai/deepseek-v4-flash-0731",
        "llm_nvidia_api_key": "nvidia-test-key",
        "admin_api_key": "admin-secret",
        "inbound_webhook_token": "webhook-secret",
        "provider_encryption_key": "encryption-secret",
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


def test_production_accepts_nvidia_nim_with_dedicated_key():
    configured = production_nvidia_settings()
    assert configured.llm_provider == "nvidia_nim"
    assert configured.llm_nvidia_reasoning_effort == "high"


def test_production_nvidia_nim_requires_dedicated_key():
    with pytest.raises(ValidationError, match="llm_nvidia_api_key"):
        production_nvidia_settings(llm_nvidia_api_key=None)
