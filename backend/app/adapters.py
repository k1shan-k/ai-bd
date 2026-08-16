import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import formataddr, parseaddr
from typing import Any, Protocol

import httpx

from app.config import Settings, get_settings


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    status: str
    accepted_at: datetime


@dataclass(frozen=True)
class BookingResult:
    provider_booking_id: str
    booking_url: str
    starts_at: datetime


class ProviderError(RuntimeError):
    """Base error for a provider operation that did not safely complete."""


class RetryableProviderError(ProviderError):
    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TerminalProviderError(ProviderError):
    """A malformed, unauthorized, policy-blocked, or invalid-recipient operation."""


class CalendarConflictError(TerminalProviderError):
    """The requested calendar slot is no longer available."""


class AmbiguousProviderError(ProviderError):
    """The provider may have accepted the operation; automatic replay is unsafe."""


class MessagingAdapter(Protocol):
    name: str

    async def check(self) -> dict[str, Any]: ...

    async def send(
        self,
        *,
        identity: str,
        body: str,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult: ...


class CalendarAdapter(Protocol):
    name: str

    async def check(self) -> dict[str, Any]: ...

    async def slots(self, *, after: datetime, timezone: str) -> list[datetime]: ...

    async def book(
        self,
        *,
        starts_at: datetime,
        timezone: str,
        idempotency_key: str,
        invitee_name: str = "Sponsor",
        invitee_email: str = "",
        lead_id: str = "",
    ) -> BookingResult: ...


class FakeMessagingAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: dict[str, SendResult] = {}

    async def check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.name, "transport": "in-memory fake"}

    async def send(
        self,
        *,
        identity: str,
        body: str,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if idempotency_key in self.sent:
            return self.sent[idempotency_key]
        digest = hashlib.sha256(f"{self.name}:{idempotency_key}".encode()).hexdigest()[:20]
        result = SendResult(
            provider_message_id=f"fake-{self.name}-{digest}",
            status="accepted",
            accepted_at=datetime.now(UTC),
        )
        self.sent[idempotency_key] = result
        return result


class FakeCalendarAdapter:
    name = "fake-calendar"

    def __init__(self) -> None:
        self.bookings: dict[str, BookingResult] = {}

    async def check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.name, "transport": "in-memory fake"}

    async def slots(self, *, after: datetime, timezone: str) -> list[datetime]:
        base = after.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return [base + timedelta(days=day) for day in range(3)]

    async def book(
        self,
        *,
        starts_at: datetime,
        timezone: str,
        idempotency_key: str,
        invitee_name: str = "Sponsor",
        invitee_email: str = "",
        lead_id: str = "",
    ) -> BookingResult:
        if idempotency_key in self.bookings:
            return self.bookings[idempotency_key]
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
        result = BookingResult(
            provider_booking_id=f"fake-booking-{digest}",
            booking_url=f"https://calendar.invalid/bookings/{digest}",
            starts_at=starts_at,
        )
        self.bookings[idempotency_key] = result
        return result


class SESMessagingAdapter:
    name = "ses"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - dependency-enabled live environment
                raise TerminalProviderError("boto3 is required for live SES") from exc
            self._client = boto3.client(
                "sesv2",
                region_name=self.settings.ses_region,
                aws_access_key_id=self.settings.aws_access_key_id,
                aws_secret_access_key=self.settings.aws_secret_access_key,
                aws_session_token=self.settings.aws_session_token,
            )
        return self._client

    async def check(self) -> dict[str, Any]:
        if not all(
            [
                self.settings.ses_region,
                self.settings.ses_sender_email,
                self.settings.ses_configuration_set,
                self.settings.ses_sns_topic_arn,
            ]
        ):
            return {
                "ok": False,
                "provider": self.name,
                "reason": "region, sender, configuration set, and SNS topic are required",
            }
        try:
            client = self._get_client()
            account, identity, destinations = await asyncio.gather(
                asyncio.to_thread(client.get_account),
                asyncio.to_thread(
                    client.get_email_identity,
                    EmailIdentity=self.settings.ses_sender_email,
                ),
                asyncio.to_thread(
                    client.get_configuration_set_event_destinations,
                    ConfigurationSetName=self.settings.ses_configuration_set,
                ),
            )
            enabled = bool(account.get("ProductionAccessEnabled"))
            verified = bool(identity.get("VerifiedForSendingStatus"))
            required_events = {
                "SEND",
                "DELIVERY",
                "BOUNCE",
                "COMPLAINT",
                "REJECT",
                "RENDERING_FAILURE",
                "DELIVERY_DELAY",
            }
            destination_ready = False
            configured_events: set[str] = set()
            for destination in destinations.get("EventDestinations") or []:
                sns = destination.get("SnsDestination") or {}
                if destination.get("Enabled") and sns.get("TopicArn") == self.settings.ses_sns_topic_arn:
                    configured_events.update(destination.get("MatchingEventTypes") or [])
                    destination_ready = required_events.issubset(configured_events)
            return {
                "ok": enabled and verified and destination_ready,
                "provider": self.name,
                "region": self.settings.ses_region,
                "production_access": enabled,
                "sender_verified": verified,
                "event_destination_ready": destination_ready,
                "missing_event_types": sorted(required_events - configured_events),
            }
        except Exception as exc:  # provider checks report rather than crash readiness UI
            return {"ok": False, "provider": self.name, "reason": str(exc)}

    async def send(
        self,
        *,
        identity: str,
        body: str,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        metadata = metadata or {}
        sender = formataddr((self.settings.ses_sender_name, self.settings.ses_sender_email or ""))
        request: dict[str, Any] = {
            "FromEmailAddress": sender,
            "Destination": {"ToAddresses": [identity]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": str(metadata.get("subject") or self.settings.ses_subject)},
                    "Body": {"Text": {"Data": body}},
                }
            },
            "EmailTags": [
                {"Name": "sponsorflow_id", "Value": idempotency_key[-240:]},
                {"Name": "lead_id", "Value": str(metadata.get("lead_id", "unknown"))},
            ],
        }
        if self.settings.ses_reply_to:
            reply_name, reply_address = parseaddr(self.settings.ses_reply_to)
            lead_id = str(metadata.get("lead_id") or "").strip()
            if lead_id and "@" in reply_address:
                local, domain = reply_address.rsplit("@", 1)
                reply_address = f"{local}+sponsorflow-{lead_id}@{domain}"
            request["ReplyToAddresses"] = [formataddr((reply_name, reply_address))]
        if self.settings.ses_configuration_set:
            request["ConfigurationSetName"] = self.settings.ses_configuration_set
        try:
            response = await asyncio.to_thread(self._get_client().send_email, **request)
        except Exception as exc:
            text = str(exc).casefold()
            if any(term in text for term in ["timeout", "connection closed", "endpointconnection"]):
                raise AmbiguousProviderError(f"SES response was ambiguous: {exc}") from exc
            if any(term in text for term in ["throttl", "too many", "service unavailable"]):
                raise RetryableProviderError(f"SES temporarily unavailable: {exc}") from exc
            raise TerminalProviderError(f"SES rejected the message: {exc}") from exc
        message_id = response.get("MessageId")
        if not message_id:
            raise AmbiguousProviderError("SES returned no MessageId")
        return SendResult(str(message_id), "accepted", datetime.now(UTC))


class TelegramMTProtoAdapter:
    name = "telegram"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._account_id: str | None = None

    async def _get_client(self) -> Any:
        async with self._lock:
            if self._client is None:
                try:
                    from telethon import TelegramClient
                    from telethon.sessions import StringSession
                except ImportError as exc:  # pragma: no cover
                    raise TerminalProviderError("Telethon is required for live Telegram") from exc
                self._client = TelegramClient(
                    StringSession(self.settings.telegram_session_string or ""),
                    self.settings.telegram_api_id,
                    self.settings.telegram_api_hash,
                    flood_sleep_threshold=0,
                )
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                raise TerminalProviderError(
                    "Telegram session is not authorized; generate SPONSORFLOW_TELEGRAM_SESSION_STRING"
                )
            return self._client

    async def _get_account_id(self, client: Any) -> str:
        if self._account_id is None:
            me = await client.get_me()
            self._account_id = str(me.id)
        return self._account_id

    async def check(self) -> dict[str, Any]:
        if not all(
            [
                self.settings.telegram_api_id,
                self.settings.telegram_api_hash,
                self.settings.telegram_session_string,
            ]
        ):
            return {"ok": False, "provider": self.name, "reason": "API/session not configured"}
        client = None
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            client = TelegramClient(
                StringSession(self.settings.telegram_session_string),
                self.settings.telegram_api_id,
                self.settings.telegram_api_hash,
                flood_sleep_threshold=0,
                receive_updates=False,
            )
            await client.connect()
            if not await client.is_user_authorized():
                return {"ok": False, "provider": self.name, "reason": "session is not authorized"}
            me = await client.get_me()
            return {
                "ok": True,
                "provider": self.name,
                "account_id": str(me.id),
                "username": me.username,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.name, "reason": str(exc)}
        finally:
            if client is not None:
                await client.disconnect()

    async def send(
        self,
        *,
        identity: str,
        body: str,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        try:
            client = await self._get_client()
            account_id = await self._get_account_id(client)
            entity = await client.get_entity(identity.lstrip("@"))
            message = await client.send_message(entity, body)
        except Exception as exc:
            seconds = getattr(exc, "seconds", None)
            if seconds is not None:
                raise RetryableProviderError(
                    f"Telegram flood wait: {seconds} seconds", int(seconds)
                ) from exc
            text = str(exc).casefold()
            if any(term in text for term in ["timeout", "connection", "disconnected"]):
                raise AmbiguousProviderError(f"Telegram response was ambiguous: {exc}") from exc
            raise TerminalProviderError(f"Telegram rejected the message: {exc}") from exc
        chat_id = getattr(message, "chat_id", None) or getattr(entity, "id", None)
        if chat_id is None:
            raise AmbiguousProviderError("Telegram returned no dialog ID")
        return SendResult(
            f"{account_id}:{chat_id}:{message.id}", "accepted", datetime.now(UTC)
        )

    async def _dispatch_telegram_message(
        self,
        message: Any,
        account_id: str,
        handler: Any,
        *,
        process_inbound: bool = True,
    ) -> None:
        sender = await message.get_sender() if not message.out else None
        username = getattr(sender, "username", None)
        chat_id = str(message.chat_id)
        await handler(
            provider_event_id=f"{account_id}:{chat_id}:{message.id}",
            provider_account_id=account_id,
            chat_id=chat_id,
            message_id=int(message.id),
            identity=str(username) if username else None,
            body=str(message.raw_text or ""),
            occurred_at=message.date,
            inbound=not bool(message.out) and process_inbound,
        )

    async def _replay_since_cursors(
        self,
        client: Any,
        account_id: str,
        handler: Any,
        cursor_getter: Any,
    ) -> None:
        async for dialog in client.iter_dialogs():
            chat_id = str(dialog.id)
            cursor = await cursor_getter(account_id, chat_id)
            if cursor is None:
                newest = [
                    message async for message in client.iter_messages(dialog.entity, limit=1)
                ]
                for message in newest:
                    await self._dispatch_telegram_message(
                        message,
                        account_id,
                        handler,
                        process_inbound=False,
                    )
                continue
            messages = [
                message
                async for message in client.iter_messages(
                    dialog.entity,
                    min_id=cursor,
                    reverse=True,
                    limit=1000,
                )
            ]
            for message in messages:
                await self._dispatch_telegram_message(message, account_id, handler)

    async def listen(self, handler: Any, cursor_getter: Any) -> None:
        try:
            from telethon import events
        except ImportError as exc:  # pragma: no cover
            raise TerminalProviderError("Telethon is required for live Telegram") from exc
        client = await self._get_client()
        account_id = await self._get_account_id(client)

        async def on_message(event: Any) -> None:
            await self._dispatch_telegram_message(event.message, account_id, handler)

        client.add_event_handler(on_message, events.NewMessage(incoming=True))
        try:
            await self._replay_since_cursors(client, account_id, handler, cursor_getter)
            await asyncio.wait_for(asyncio.shield(client.disconnected), timeout=30)
        except TimeoutError:
            return
        finally:
            client.remove_event_handler(on_message)


class WhatsAppCloudAdapter:
    name = "whatsapp"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=20.0)

    @property
    def endpoint(self) -> str:
        return (
            f"https://graph.facebook.com/{self.settings.whatsapp_graph_version}/"
            f"{self.settings.whatsapp_phone_number_id}"
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.whatsapp_access_token}"}

    async def check(self) -> dict[str, Any]:
        if not all(
            [
                self.settings.whatsapp_graph_version,
                self.settings.whatsapp_phone_number_id,
                self.settings.whatsapp_access_token,
                self.settings.whatsapp_template_name,
            ]
        ):
            return {"ok": False, "provider": self.name, "reason": "Cloud API not configured"}
        try:
            response = await self.client.get(
                self.endpoint,
                headers=self.headers,
                params={"fields": "id,display_phone_number,verified_name"},
            )
            response.raise_for_status()
            return {"ok": True, "provider": self.name, **response.json()}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "reason": str(exc)}

    async def send(
        self,
        *,
        identity: str,
        body: str,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        metadata = metadata or {}
        action_type = metadata.get("action_type")
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": identity.lstrip("+"),
        }
        if action_type == "whatsapp_fallback":
            template: dict[str, Any] = {
                "name": self.settings.whatsapp_template_name,
                "language": {"code": self.settings.whatsapp_template_language},
            }
            if self.settings.whatsapp_template_body_mode == "message_body":
                template["components"] = [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": body}],
                    }
                ]
            payload.update({"type": "template", "template": template})
        else:
            payload.update({"type": "text", "text": {"preview_url": False, "body": body}})
        try:
            response = await self.client.post(
                f"{self.endpoint}/messages",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AmbiguousProviderError(f"WhatsApp response was ambiguous: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("retry-after")
            raise RetryableProviderError(
                f"WhatsApp temporary error {response.status_code}: {response.text[:500]}",
                int(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code >= 400:
            raise TerminalProviderError(
                f"WhatsApp rejected the message {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        message_id = (data.get("messages") or [{}])[0].get("id")
        if not message_id:
            raise AmbiguousProviderError("WhatsApp returned no message ID")
        return SendResult(str(message_id), "accepted", datetime.now(UTC))


class CalComCalendarAdapter:
    name = "calcom"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=20.0)

    @property
    def headers(self) -> dict[str, str]:
        key = self.settings.calcom_api_key or self.settings.calendar_api_key
        return {
            "Authorization": f"Bearer {key}",
            "cal-api-version": self.settings.calcom_api_version,
            "Content-Type": "application/json",
        }

    async def check(self) -> dict[str, Any]:
        if not (self.settings.calcom_api_key or self.settings.calendar_api_key):
            return {"ok": False, "provider": self.name, "reason": "API key not configured"}
        if not self.settings.calcom_event_type_id:
            return {"ok": False, "provider": self.name, "reason": "event type not configured"}
        try:
            response = await self.client.get(
                f"{self.settings.calcom_base_url}/event-types/{self.settings.calcom_event_type_id}",
                headers=self.headers,
            )
            response.raise_for_status()
            return {"ok": True, "provider": self.name, "event_type_id": self.settings.calcom_event_type_id}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "reason": str(exc)}

    async def slots(self, *, after: datetime, timezone: str) -> list[datetime]:
        end = after + timedelta(days=14)
        response = await self.client.get(
            f"{self.settings.calcom_base_url}/slots",
            headers=self.headers,
            params={
                "eventTypeId": self.settings.calcom_event_type_id,
                "startTime": after.isoformat(),
                "endTime": end.isoformat(),
                "timeZone": timezone,
            },
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableProviderError(f"Cal.com temporary error: {response.text[:500]}")
        if response.status_code >= 400:
            raise TerminalProviderError(f"Cal.com slot query failed: {response.text[:500]}")
        raw = response.json().get("data", response.json())
        values: list[str] = []
        if isinstance(raw, dict):
            for group in raw.values():
                if isinstance(group, list):
                    for slot in group:
                        value = slot.get("start") if isinstance(slot, dict) else slot
                        if isinstance(value, str):
                            values.append(value)
        elif isinstance(raw, list):
            for slot in raw:
                value = slot.get("start") if isinstance(slot, dict) else slot
                if isinstance(value, str):
                    values.append(value)
        parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
        return sorted(parsed)[:3]

    async def book(
        self,
        *,
        starts_at: datetime,
        timezone: str,
        idempotency_key: str,
        invitee_name: str = "Sponsor",
        invitee_email: str = "",
        lead_id: str = "",
    ) -> BookingResult:
        payload = {
            "start": starts_at.isoformat(),
            "eventTypeId": self.settings.calcom_event_type_id,
            "attendee": {
                "name": invitee_name,
                "email": invitee_email,
                "timeZone": timezone,
                "language": "en",
            },
            "metadata": {
                "sponsorflowLeadId": lead_id,
                "sponsorflowIdempotencyKey": idempotency_key,
            },
        }
        try:
            response = await self.client.post(
                f"{self.settings.calcom_base_url}/bookings", headers=self.headers, json=payload
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AmbiguousProviderError(f"Cal.com booking response was ambiguous: {exc}") from exc
        if response.status_code == 409:
            raise CalendarConflictError("Cal.com slot is no longer available")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableProviderError(f"Cal.com temporary error: {response.text[:500]}")
        if response.status_code >= 400:
            raise TerminalProviderError(f"Cal.com booking failed: {response.text[:500]}")
        data = response.json().get("data", response.json())
        booking_id = data.get("uid") or data.get("id")
        if not booking_id:
            raise AmbiguousProviderError("Cal.com returned no booking ID")
        booking_url = data.get("meetingUrl") or data.get("bookingUrl") or data.get("url") or ""
        start_value = data.get("start") or starts_at.isoformat()
        return BookingResult(
            provider_booking_id=str(booking_id),
            booking_url=str(booking_url),
            starts_at=datetime.fromisoformat(str(start_value).replace("Z", "+00:00")),
        )


class AdapterRegistry:
    def __init__(self, settings: Settings) -> None:
        self.base_settings = settings
        self.settings = settings
        self.revision = "environment"
        self._refresh_lock = asyncio.Lock()
        self._install(settings)

    def _install(self, settings: Settings) -> None:
        self.settings = settings
        if settings.provider_mode == "fake":
            self.messaging: dict[str, MessagingAdapter] = {
                "email": FakeMessagingAdapter("ses"),
                "telegram": FakeMessagingAdapter("telegram"),
                "whatsapp": FakeMessagingAdapter("whatsapp"),
            }
            self.calendar: CalendarAdapter = FakeCalendarAdapter()
        else:
            self.messaging = {
                "email": SESMessagingAdapter(settings),
                "telegram": TelegramMTProtoAdapter(settings),
                "whatsapp": WhatsAppCloudAdapter(settings),
            }
            self.calendar = CalComCalendarAdapter(settings)

    async def refresh(self, session: Any, *, force: bool = False) -> bool:
        async with self._refresh_lock:
            return await self._refresh_locked(session, force=force)

    async def _refresh_locked(self, session: Any, *, force: bool) -> bool:
        from app.provider_config import settings_from_store

        candidate, revision = settings_from_store(session, self.base_settings)
        if not force and revision == self.revision:
            return False
        old_messaging = list(self.messaging.values())
        old_calendar = self.calendar
        self._install(candidate)
        self.revision = revision
        for adapter in old_messaging:
            client = getattr(adapter, "_client", None)
            if client is not None and hasattr(client, "disconnect"):
                await client.disconnect()
            http_client = getattr(adapter, "client", None)
            if http_client is not None and hasattr(http_client, "aclose"):
                await http_client.aclose()
        calendar_client = getattr(old_calendar, "client", None)
        if calendar_client is not None and hasattr(calendar_client, "aclose"):
            await calendar_client.aclose()
        return True

    def configuration_errors(self) -> list[str]:
        if self.settings.provider_mode == "fake":
            return []
        required = {
            "ses_region": self.settings.ses_region,
            "ses_sender_email": self.settings.ses_sender_email,
            "ses_reply_to": self.settings.ses_reply_to,
            "ses_sns_topic_arn": self.settings.ses_sns_topic_arn,
            "ses_configuration_set": self.settings.ses_configuration_set,
            "telegram_api_id": self.settings.telegram_api_id,
            "telegram_api_hash": self.settings.telegram_api_hash,
            "telegram_session_string": self.settings.telegram_session_string,
            "whatsapp_access_token": self.settings.whatsapp_access_token,
            "whatsapp_phone_number_id": self.settings.whatsapp_phone_number_id,
            "whatsapp_graph_version": self.settings.whatsapp_graph_version,
            "whatsapp_template_name": self.settings.whatsapp_template_name,
            "whatsapp_app_secret": self.settings.whatsapp_app_secret,
            "whatsapp_verify_token": self.settings.whatsapp_verify_token,
            "calcom_api_key": self.settings.calcom_api_key or self.settings.calendar_api_key,
            "calcom_event_type_id": self.settings.calcom_event_type_id,
            "calcom_webhook_secret": self.settings.calcom_webhook_secret,
            "tavily_api_key": self.settings.tavily_api_key,
        }
        return sorted(name for name, value in required.items() if not value)

    async def _research_check(self) -> dict[str, Any]:
        if self.settings.research_provider == "fake":
            return {"ok": True, "provider": "fake"}
        if not self.settings.tavily_api_key:
            return {"ok": False, "provider": "tavily", "reason": "API key not configured"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.settings.tavily_base_url.rstrip('/')}/usage",
                    headers={"Authorization": f"Bearer {self.settings.tavily_api_key}"},
                )
            response.raise_for_status()
            return {"ok": True, "provider": "tavily", "credential_valid": True}
        except Exception as exc:
            return {"ok": False, "provider": "tavily", "reason": str(exc)}

    async def checks(self) -> list[dict[str, Any]]:
        checks = []
        for channel, adapter in self.messaging.items():
            details = await adapter.check()
            checks.append(
                {
                    "provider": adapter.name,
                    "configured": bool(details.get("ok")),
                    "mode": self.settings.provider_mode,
                    "details": {**details, "channel": channel},
                }
            )
        calendar_details = await self.calendar.check()
        checks.append(
            {
                "provider": self.calendar.name,
                "configured": bool(calendar_details.get("ok")),
                "mode": self.settings.provider_mode,
                "details": calendar_details,
            }
        )
        research_details = await self._research_check()
        checks.append(
            {
                "provider": self.settings.research_provider,
                "configured": bool(research_details.get("ok")),
                "mode": self.settings.provider_mode,
                "details": research_details,
            }
        )
        return checks


registry = AdapterRegistry(get_settings())
