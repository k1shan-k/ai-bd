import base64
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

_FIELD_ORDER = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": [
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ],
    "UnsubscribeConfirmation": [
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ],
}
_LEAD_ADDRESS = re.compile(r"\+sponsorflow-([0-9a-f-]{36})@", re.IGNORECASE)
_MAX_MIME_BYTES = 150 * 1024
_MAX_BODY_CHARS = 50_000


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _canonical_message(payload: dict[str, Any]) -> bytes:
    fields = _FIELD_ORDER.get(str(payload.get("Type")))
    if not fields:
        raise ValueError("unsupported SNS message type")
    parts: list[str] = []
    for field in fields:
        if field == "Subject" and field not in payload:
            continue
        if field not in payload:
            raise ValueError(f"SNS message is missing {field}")
        parts.extend([field, str(payload[field])])
    return ("\n".join(parts) + "\n").encode()


def _valid_sns_url(value: str, *, certificate: bool = False) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if not (
        parsed.scheme == "https"
        and parsed.port in {None, 443}
        and hostname.startswith("sns.")
        and hostname.endswith(".amazonaws.com")
    ):
        return False
    return not certificate or parsed.path.endswith(".pem")


async def verify_sns_signature(payload: dict[str, Any]) -> None:
    cert_url = str(payload.get("SigningCertURL") or "")
    if not _valid_sns_url(cert_url, certificate=True):
        raise ValueError("invalid SNS signing certificate URL")
    version = str(payload.get("SignatureVersion") or "")
    algorithm = hashes.SHA1() if version == "1" else hashes.SHA256() if version == "2" else None
    if algorithm is None:
        raise ValueError("unsupported SNS signature version")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(cert_url)
        response.raise_for_status()
        certificate = x509.load_pem_x509_certificate(response.content)
        signature = base64.b64decode(str(payload["Signature"]), validate=True)
        certificate.public_key().verify(
            signature,
            _canonical_message(payload),
            padding.PKCS1v15(),
            algorithm,
        )
    except Exception as exc:
        raise ValueError("invalid SNS signature") from exc


async def confirm_sns_subscription(payload: dict[str, Any]) -> None:
    url = str(payload.get("SubscribeURL") or "")
    if not _valid_sns_url(url):
        raise ValueError("invalid SNS subscription confirmation URL")
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ValueError("SNS subscription confirmation failed") from exc


def parse_ses_received_email(event: dict[str, Any]) -> dict[str, str | None]:
    content = event.get("content")
    mail = event.get("mail") or {}
    if not isinstance(content, str) or not content:
        raise ValueError("SES receipt notification does not contain raw MIME content")
    raw = content.encode("utf-8", errors="replace")
    if len(raw) > _MAX_MIME_BYTES:
        raise ValueError("SES inbound email exceeds the configured MIME limit")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise ValueError("SES inbound MIME could not be parsed") from exc

    sender_addresses = getaddresses(message.get_all("from", []))
    header_sender = next(
        (address.strip().casefold() for _, address in sender_addresses if address), ""
    )
    sender = str(mail.get("source") or "").strip().casefold()
    if not sender:
        raise ValueError("SES inbound email has no envelope sender")
    if header_sender and header_sender != sender:
        raise ValueError("SES envelope sender does not match the MIME From address")
    recipients = [address for _, address in getaddresses(message.get_all("to", []))]
    recipients.extend(str(value) for value in mail.get("destination") or [])
    lead_id = None
    for recipient in recipients:
        match = _LEAD_ADDRESS.search(recipient)
        if match:
            lead_id = match.group(1).lower()
            break

    selected = message.get_body(preferencelist=("plain", "html"))
    if selected is None and not message.is_multipart():
        selected = message
    if selected is None:
        raise ValueError("SES inbound email has no readable body")
    try:
        body = selected.get_content()
    except (LookupError, UnicodeError) as exc:
        raise ValueError("SES inbound email body encoding is unsupported") from exc
    if selected.get_content_type() == "text/html":
        extractor = _TextExtractor()
        extractor.feed(str(body))
        body = " ".join(extractor.parts)
    normalized = "\n".join(line.rstrip() for line in str(body).splitlines()).strip()
    if not normalized:
        raise ValueError("SES inbound email body is empty")
    return {
        "provider_event_id": str(mail.get("messageId") or message.get("Message-ID") or ""),
        "identity": sender,
        "lead_id": lead_id,
        "body": normalized[:_MAX_BODY_CHARS],
    }
