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
