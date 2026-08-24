import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeVar
from urllib.parse import quote, urlparse

import boto3
import httpx
from botocore.config import Config as BotoConfig
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, get_settings


class LLMError(RuntimeError):
    """Base class for safe model failures."""


class LLMConfigurationError(LLMError):
    """The selected provider is not configured."""


class LLMUnavailableError(LLMError):
    """The provider did not produce a definitive response."""


class LLMResponseError(LLMError):
    """The provider response was not valid for the requested schema."""


class OutreachDraft(BaseModel):
    subject: str | None = Field(default=None, max_length=180)
    body: str = Field(min_length=1, max_length=6_000)
    personalization_fact_ids: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0, le=1)
    requires_human_review: bool = False
    review_reasons: list[str] = Field(default_factory=list, max_length=12)


class ReplyInterpretation(BaseModel):
    primary_intent: Literal[
        "opt_out",
        "negative",
        "interested",
        "interested_with_tier",
        "question",
        "objection",
        "call_request",
        "meeting_selection",
        "complaint",
        "wrong_person",
        "uncertain",
    ]
    secondary_intents: list[str] = Field(default_factory=list, max_length=8)
    package_id: str | None = None
    question_topics: list[str] = Field(default_factory=list, max_length=12)
    objections: list[str] = Field(default_factory=list, max_length=12)
    sentiment: Literal["positive", "neutral", "negative", "mixed"] = "neutral"
    confidence: float = Field(ge=0, le=1)
    requires_human_review: bool = False
    reason: str = Field(default="", max_length=1_000)


class ConversationDraft(BaseModel):
    body: str = Field(min_length=1, max_length=6_000)
    answered_topics: list[str] = Field(default_factory=list, max_length=12)
    unanswered_topics: list[str] = Field(default_factory=list, max_length=12)
    proposed_next_step: Literal[
        "clarify",
        "share_packages",
        "handle_objection",
        "offer_meeting",
        "close_loop",
        "human_review",
    ]
    confidence: float = Field(ge=0, le=1)
    requires_human_review: bool = False
    review_reasons: list[str] = Field(default_factory=list, max_length=12)


class ResearchFact(BaseModel):
    fact_id: str = Field(min_length=1, max_length=80)
    claim: str = Field(min_length=1, max_length=1_000)
    source_url: str = Field(min_length=1, max_length=2_000)
    source_excerpt: str = Field(default="", max_length=2_000)
    confidence: float = Field(ge=0, le=1)


class ResearchFitAngle(BaseModel):
    angle: str = Field(min_length=1, max_length=1_000)
    supporting_fact_ids: list[str] = Field(default_factory=list, max_length=8)


class ResearchSynthesis(BaseModel):
    company_summary: str = Field(max_length=3_000)
    prospect_summary: str = Field(max_length=2_000)
    verified_facts: list[ResearchFact] = Field(default_factory=list, max_length=20)
    fit_angles: list[ResearchFitAngle] = Field(default_factory=list, max_length=8)
    risk_flags: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0, le=1)


class MemoryUpdate(BaseModel):
    summary: str = Field(max_length=6_000)
    goals: list[str] = Field(default_factory=list, max_length=12)
    package_interest: list[str] = Field(default_factory=list, max_length=8)
    objections: list[str] = Field(default_factory=list, max_length=12)
    answered_questions: list[str] = Field(default_factory=list, max_length=20)
    open_questions: list[str] = Field(default_factory=list, max_length=20)
    commitments: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class RawCompletion:
    content: str | dict[str, Any]
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> RawCompletion: ...


TModel = TypeVar("TModel", bound=BaseModel)


@dataclass(frozen=True)
class StructuredResult(Generic[TModel]):
    value: TModel
    provider: str
    model: str
    operation: str
    attempts: int
    latency_ms: int
    usage: dict[str, Any]
    prompt_hash: str


def _json_from_content(content: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMResponseError("model response did not contain a JSON object") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMResponseError("model response contained invalid JSON") from exc
    if not isinstance(value, dict):
        raise LLMResponseError("model response must be a JSON object")
    return value


def _schema_prompt(prompt: str, schema: dict[str, Any]) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        "Return exactly one JSON object and no markdown. It must satisfy this JSON Schema:\n"
        f"{json.dumps(schema, separators=(',', ':'), ensure_ascii=False)}"
    )


class LLMClient:
    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings

    async def generate(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        output_model: type[TModel],
        temperature: float,
        max_tokens: int | None = None,
    ) -> StructuredResult[TModel]:
        if len(system) + len(prompt) > self.settings.llm_max_input_chars:
            raise LLMResponseError("LLM input exceeds the configured character limit")
        schema = output_model.model_json_schema()
        final_prompt = _schema_prompt(prompt, schema)
        prompt_hash = hashlib.sha256(f"{operation}\0{system}\0{final_prompt}".encode()).hexdigest()
        started = time.perf_counter()
        last_error: Exception | None = None
        attempts = self.settings.llm_max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                raw = await asyncio.wait_for(
                    self.provider.complete(
                        operation=operation,
                        system=system,
                        prompt=final_prompt,
                        schema=schema,
                        temperature=temperature,
                        max_tokens=max_tokens or self.settings.llm_max_output_tokens,
                    ),
                    timeout=self.settings.llm_timeout_seconds,
                )
                try:
                    value = output_model.model_validate(_json_from_content(raw.content))
                except (ValidationError, LLMResponseError) as exc:
                    raise LLMResponseError(
                        f"model response failed {output_model.__name__} validation"
                    ) from exc
                return StructuredResult(
                    value=value,
                    provider=raw.provider,
                    model=raw.model,
                    operation=operation,
                    attempts=attempt,
                    latency_ms=int((time.perf_counter() - started) * 1_000),
                    usage=raw.usage,
                    prompt_hash=prompt_hash,
                )
            except LLMConfigurationError:
                raise
            except (TimeoutError, LLMUnavailableError, LLMResponseError) as exc:
                last_error = exc
            except Exception as exc:  # Provider SDKs expose many transient exception classes.
                last_error = LLMUnavailableError(type(exc).__name__)
            if attempt < attempts:
                await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
        if isinstance(last_error, LLMResponseError):
            raise last_error
        raise LLMUnavailableError(
            f"{self.provider.name} failed after {attempts} attempt(s)"
        ) from last_error


class GatewayLLMProvider:
    """OpenAI-compatible adapter for pinealctx/kiro-gateway or another local gateway."""

    name = "gateway"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_gateway_url:
            raise LLMConfigurationError("llm_gateway_url is not configured")
        if not settings.llm_model:
            raise LLMConfigurationError("llm_model is not configured")
        self.settings = settings
        self.model = settings.llm_model

    def _url(self) -> str:
        base = self.settings.llm_gateway_url.rstrip("/")
        path = self.settings.llm_gateway_path
        return f"{base}/{path.lstrip('/')}"

    async def complete(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> RawCompletion:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        if self.settings.llm_gateway_protocol == "simple":
            payload: dict[str, Any] = {
                "operation": operation,
                "model": self.model,
                "system": system,
                "prompt": prompt,
                "schema": schema,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        else:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                response = await client.post(self._url(), headers=headers, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"gateway request failed: {type(exc).__name__}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError("gateway returned invalid JSON") from exc
        if self.settings.llm_gateway_protocol == "simple":
            content = data.get("output", data.get("content", data.get("response")))
        else:
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise LLMResponseError("gateway response has no choices")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, (str, dict)):
            raise LLMResponseError("gateway response has no usable content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return RawCompletion(content=content, provider=self.name, model=self.model, usage=usage)


class VertexMaaSLLMProvider:
    """Vertex AI MaaS adapter for OpenAI-compatible managed open-model endpoints."""

    name = "vertex-maas"
    _metadata_url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    )

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_vertex_project_id:
            raise LLMConfigurationError("llm_vertex_project_id is not configured")
        if not settings.llm_model:
            raise LLMConfigurationError("llm_model is not configured")
        if settings.llm_vertex_auth_mode == "access_token" and not settings.llm_vertex_access_token:
            raise LLMConfigurationError("llm_vertex_access_token is not configured")
        if settings.environment == "production" and settings.llm_vertex_auth_mode != "metadata":
            raise LLMConfigurationError(
                "production Vertex MaaS requires metadata service-account authentication"
            )
        self.settings = settings
        self.model = settings.llm_model
        self._cached_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._url = self._endpoint_url()

    def _endpoint_url(self) -> str:
        endpoint = self.settings.llm_vertex_endpoint or (
            f"{self.settings.llm_vertex_region}-aiplatform.googleapis.com"
        )
        if "://" not in endpoint:
            endpoint = f"https://{endpoint}"
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise LLMConfigurationError("llm_vertex_endpoint must be an HTTPS origin")
        project = quote(self.settings.llm_vertex_project_id or "", safe="")
        region = quote(self.settings.llm_vertex_region, safe="")
        return (
            f"https://{parsed.netloc}/{self.settings.llm_vertex_api_version}/projects/"
            f"{project}/locations/{region}/endpoints/openapi/chat/completions"
        )

    @staticmethod
    def _validated_token(value: str | None) -> str:
        token = (value or "").strip()
        if not token or len(token) > 8_192 or any(character.isspace() for character in token):
            raise LLMConfigurationError("Vertex MaaS authentication did not return a valid token")
        return token

    async def _gcloud_command_token(self, *arguments: str) -> tuple[str, int]:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "gcloud",
                *arguments,
                "--quiet",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=min(15.0, self.settings.llm_timeout_seconds)
            )
        except FileNotFoundError as exc:
            raise LLMConfigurationError(
                "gcloud is unavailable for local Vertex authentication"
            ) from exc
        except TimeoutError as exc:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise LLMUnavailableError("gcloud token acquisition timed out") from exc
        if process.returncode != 0:
            raise LLMConfigurationError("gcloud could not acquire a Vertex access token")
        return (
            self._validated_token(stdout.decode("utf-8", errors="ignore")),
            self.settings.llm_vertex_token_ttl_seconds,
        )

    async def _gcloud_token(self) -> tuple[str, int]:
        return await self._gcloud_command_token("auth", "print-access-token")

    async def _adc_token(self) -> tuple[str, int]:
        return await self._gcloud_command_token("auth", "application-default", "print-access-token")

    async def _metadata_token(self) -> tuple[str, int]:
        try:
            async with httpx.AsyncClient(
                timeout=min(5.0, self.settings.llm_timeout_seconds), trust_env=False
            ) as client:
                response = await client.get(
                    self._metadata_url, headers={"Metadata-Flavor": "Google"}
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"Vertex metadata token request failed: {type(exc).__name__}"
            ) from exc
        try:
            data = response.json()
            token = self._validated_token(data.get("access_token"))
            expires_in = int(data.get("expires_in", 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise LLMResponseError("Vertex metadata service returned invalid token data") from exc
        if expires_in <= 0:
            raise LLMResponseError("Vertex metadata service returned an invalid token lifetime")
        return token, max(1, expires_in - 60)

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        if self.settings.llm_vertex_auth_mode == "access_token":
            return self._validated_token(self.settings.llm_vertex_access_token)
        now = time.monotonic()
        if not force_refresh and self._cached_token and now < self._token_expires_at:
            return self._cached_token
        async with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._cached_token and now < self._token_expires_at:
                return self._cached_token
            if self.settings.llm_vertex_auth_mode == "gcloud":
                token, ttl = await self._gcloud_token()
            elif self.settings.llm_vertex_auth_mode == "adc":
                token, ttl = await self._adc_token()
            else:
                token, ttl = await self._metadata_token()
            self._cached_token = token
            self._token_expires_at = time.monotonic() + ttl
            return token

    async def complete(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> RawCompletion:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": (f"AUTHORITATIVE SYSTEM POLICY:\n{system}\n\nTASK:\n{prompt}"),
                }
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        response: httpx.Response | None = None
        for auth_attempt in range(2):
            token = await self._access_token(force_refresh=auth_attempt > 0)
            try:
                async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                    response = await client.post(
                        self._url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except httpx.HTTPError as exc:
                raise LLMUnavailableError(
                    f"Vertex MaaS request failed: {type(exc).__name__}"
                ) from exc
            if (
                response.status_code in {401, 403}
                and self.settings.llm_vertex_auth_mode != "access_token"
                and auth_attempt == 0
            ):
                self._cached_token = None
                self._token_expires_at = 0.0
                continue
            break
        if response is None:
            raise LLMUnavailableError("Vertex MaaS did not return a response")
        if response.status_code in {401, 403}:
            raise LLMConfigurationError(
                f"Vertex MaaS authorization failed with HTTP {response.status_code}"
            )
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise LLMConfigurationError(
                f"Vertex MaaS request configuration was rejected with HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise LLMUnavailableError(
                f"Vertex MaaS request failed with HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError("Vertex MaaS returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("Vertex MaaS response must be a JSON object")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("Vertex MaaS response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, (str, dict)):
            raise LLMResponseError("Vertex MaaS response has no usable content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return RawCompletion(content=content, provider=self.name, model=self.model, usage=usage)


class BedrockLLMProvider:
    name = "bedrock"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_model:
            raise LLMConfigurationError("llm_model is not configured")
        self.settings = settings
        self.model = settings.llm_model
        kwargs: dict[str, Any] = {
            "region_name": settings.llm_bedrock_region,
            "config": BotoConfig(
                connect_timeout=settings.llm_timeout_seconds,
                read_timeout=settings.llm_timeout_seconds,
                retries={"max_attempts": 0},
            ),
        }
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            kwargs.update(
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                aws_session_token=settings.aws_session_token,
            )
        self._client = boto3.client("bedrock-runtime", **kwargs)

    async def complete(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> RawCompletion:
        def call() -> dict[str, Any]:
            return self._client.converse(
                modelId=self.model,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )

        try:
            data = await asyncio.to_thread(call)
        except Exception as exc:
            raise LLMUnavailableError(f"Bedrock request failed: {type(exc).__name__}") from exc
        content_items = data.get("output", {}).get("message", {}).get("content", [])
        text = "".join(
            str(item.get("text", "")) for item in content_items if isinstance(item, dict)
        ).strip()
        if not text:
            raise LLMResponseError("Bedrock response has no text content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return RawCompletion(content=text, provider=self.name, model=self.model, usage=usage)


class FakeLLMProvider:
    name = "fake-llm"
    model = "fake-bd-v1"

    def __init__(self, responses: dict[str, list[dict[str, Any] | str]] | None = None) -> None:
        self.responses: dict[str, deque[dict[str, Any] | str]] = defaultdict(deque)
        for operation, values in (responses or {}).items():
            self.responses[operation].extend(values)
        self.calls: list[dict[str, Any]] = []

    def queue(self, operation: str, response: dict[str, Any] | str) -> None:
        self.responses[operation].append(response)

    async def complete(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> RawCompletion:
        self.calls.append(
            {
                "operation": operation,
                "system": system,
                "prompt": prompt,
                "schema": schema,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.responses[operation]:
            content = self.responses[operation].popleft()
        else:
            content = self._default(operation, prompt)
        return RawCompletion(content=content, provider=self.name, model=self.model)

    @staticmethod
    def _default(operation: str, prompt: str = "") -> dict[str, Any]:
        if operation == "outreach":
            data: dict[str, Any] = {}
            marker = "INPUT_DATA="
            if marker in prompt:
                try:
                    data, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
                except (json.JSONDecodeError, TypeError):
                    data = {}
            channel = str(data.get("channel") or "")
            action = str(data.get("action") or "")
            prospect = data.get("prospect") if isinstance(data.get("prospect"), dict) else {}
            event_kit = data.get("event_kit") if isinstance(data.get("event_kit"), dict) else {}
            event = event_kit.get("event") if isinstance(event_kit.get("event"), dict) else {}
            name = str(prospect.get("full_name") or "there")
            company = str(prospect.get("company") or "your team")
            event_name = str(event.get("name") or "the event")
            if action.startswith("initial"):
                body = (
                    f"Hi {name} — thanks for indicating that {company} may be interested in "
                    f"sponsoring {event_name}. Would a concise overview of the relevant options be useful?"
                )
            else:
                body = (
                    f"Hi {name} — a quick follow-up on sponsorship for {event_name}. "
                    "Would package details be useful, or should I close the loop?"
                )
            if channel == "email":
                body += "\n\nBest,\nThe Sponsorship Team"
            return {
                "subject": f"{event_name} sponsorship" if channel == "email" else None,
                "body": body,
                "personalization_fact_ids": [],
                "confidence": 0.8,
                "requires_human_review": False,
                "review_reasons": [],
            }
        if operation == "interpret_reply":
            data: dict[str, Any] = {}
            marker = "INPUT_DATA="
            if marker in prompt:
                try:
                    data, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
                except (json.JSONDecodeError, TypeError):
                    data = {}
            text = str(data.get("inbound_message") or "").casefold()
            packages = data.get("packages") if isinstance(data.get("packages"), list) else []
            package_id = next(
                (
                    str(item.get("id"))
                    for item in packages
                    if isinstance(item, dict)
                    and (
                        str(item.get("id") or "").casefold() in text
                        or str(item.get("name") or "").casefold() in text
                    )
                ),
                None,
            )
            if any(
                term in text
                for term in (
                    "jump on a call",
                    "book a call",
                    "let's talk",
                    "lets talk",
                    "ready to talk",
                )
            ):
                intent = "call_request"
            elif package_id and any(
                term in text for term in ("interested", "send details", "sounds good")
            ):
                intent = "interested_with_tier"
            elif any(
                term in text for term in ("interested", "tell me more", "send details", "sponsor")
            ):
                intent = "interested"
            elif "?" in text or any(
                term in text for term in ("price", "cost", "package", "benefit", "perk")
            ):
                intent = "question"
            else:
                intent = "uncertain"
            return {
                "primary_intent": intent,
                "secondary_intents": [],
                "package_id": package_id,
                "question_topics": ["packages"] if intent == "question" else [],
                "objections": [],
                "sentiment": "positive" if intent.startswith("interested") else "neutral",
                "confidence": 0.95 if intent != "uncertain" else 0.4,
                "requires_human_review": intent == "uncertain",
                "reason": "Deterministic fake LLM interpretation.",
            }
        if operation == "conversation_reply":
            data: dict[str, Any] = {}
            marker = "INPUT_DATA="
            if marker in prompt:
                try:
                    data, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
                except (json.JSONDecodeError, TypeError):
                    data = {}
            interpretation = (
                data.get("interpretation") if isinstance(data.get("interpretation"), dict) else {}
            )
            intent = str(interpretation.get("primary_intent") or "uncertain")
            event_kit = data.get("event_kit") if isinstance(data.get("event_kit"), dict) else {}
            packages = (
                event_kit.get("packages") if isinstance(event_kit.get("packages"), list) else []
            )
            slots = data.get("authoritative_meeting_slots") or []
            if slots and intent in {"call_request", "interested_with_tier"}:
                body = (
                    "Happy to talk. The available times are: "
                    + ", ".join(str(x) for x in slots)
                    + ". Which works best?"
                )
                step = "offer_meeting"
            elif intent == "question" and packages:
                summary = "; ".join(
                    f"{item.get('name')} ({item.get('list_price')})"
                    for item in packages
                    if isinstance(item, dict)
                )
                body = f"The current sponsorship options are {summary}. Which outcome matters most to your team?"
                step = "share_packages"
            elif intent in {"interested", "interested_with_tier"}:
                body = "Great to hear. Which sponsorship outcome or package is closest to what you have in mind?"
                step = "clarify"
            elif intent == "objection":
                body = "That makes sense. Which part feels furthest from what your team needs so I can answer specifically?"
                step = "handle_objection"
            elif intent == "negative":
                body = "Understood — thanks for letting me know. I’ll close the loop here."
                step = "close_loop"
            else:
                body = "Thanks for the context. I want to verify that before giving you the wrong answer."
                step = "human_review"
            return {
                "body": body,
                "answered_topics": list(interpretation.get("question_topics") or []),
                "unanswered_topics": [],
                "proposed_next_step": step,
                "confidence": 0.92 if step != "human_review" else 0.4,
                "requires_human_review": step == "human_review",
                "review_reasons": [] if step != "human_review" else ["No safe fake response."],
            }
        if operation == "research_synthesis":
            data: dict[str, Any] = {}
            marker = "INPUT_DATA="
            if marker in prompt:
                try:
                    data, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
                except (json.JSONDecodeError, TypeError):
                    data = {}
            prospect = (
                data.get("prospect_from_csv")
                if isinstance(data.get("prospect_from_csv"), dict)
                else {}
            )
            sources = (
                data.get("public_sources") if isinstance(data.get("public_sources"), list) else []
            )
            company = str(prospect.get("company") or "the organisation")
            facts = []
            for index, source in enumerate(sources[:4]):
                if not isinstance(source, dict) or not source.get("url"):
                    continue
                facts.append(
                    {
                        "fact_id": f"fact-{index + 1}",
                        "claim": str(
                            source.get("title")
                            or source.get("excerpt")
                            or f"Public profile for {company}"
                        ),
                        "source_url": str(source["url"]),
                        "source_excerpt": str(source.get("excerpt") or ""),
                        "confidence": float(source.get("source_confidence") or 0.9),
                    }
                )
            return {
                "company_summary": f"Source-backed profile for {company}.",
                "prospect_summary": f"The CSV lists the prospect in a relevant role at {company}.",
                "verified_facts": facts,
                "fit_angles": [
                    {
                        "angle": f"Connect {company}'s documented work to the event audience.",
                        "supporting_fact_ids": [facts[0]["fact_id"]],
                    }
                ]
                if facts
                else [],
                "risk_flags": [],
                "confidence": 0.9 if facts else 0.2,
            }
        if operation == "memory_update":
            return {
                "summary": "",
                "goals": [],
                "package_interest": [],
                "objections": [],
                "answered_questions": [],
                "open_questions": [],
                "commitments": [],
                "confidence": 0.2,
            }
        raise LLMResponseError(f"fake LLM has no default for operation {operation}")


class DisabledLLMProvider:
    name = "disabled"
    model = "disabled"

    async def complete(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> RawCompletion:
        raise LLMConfigurationError("LLM provider is disabled")


class NvidiaNIMLLMProvider:
    """NVIDIA-hosted NIM adapter for its OpenAI-compatible chat API."""

    name = "nvidia-nim"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_model:
            raise LLMConfigurationError("llm_model is not configured")
        api_key = (settings.llm_nvidia_api_key or "").strip()
        if not api_key or len(api_key) > 8_192 or any(character.isspace() for character in api_key):
            raise LLMConfigurationError("llm_nvidia_api_key is not configured")
        self.settings = settings
        self.model = settings.llm_model
        self._url = settings.llm_nvidia_endpoint
        self._api_key = api_key

    async def complete(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> RawCompletion:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "top_p": self.settings.llm_nvidia_top_p,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {
                "thinking": self.settings.llm_nvidia_thinking,
                "reasoning_effort": self.settings.llm_nvidia_reasoning_effort,
            },
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                response = await client.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"NVIDIA NIM request failed: {type(exc).__name__}") from exc
        if response.status_code in {401, 403}:
            raise LLMConfigurationError(
                f"NVIDIA NIM authorization failed with HTTP {response.status_code}"
            )
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise LLMConfigurationError(
                f"NVIDIA NIM request configuration was rejected with HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise LLMUnavailableError(f"NVIDIA NIM request failed with HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError("NVIDIA NIM returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("NVIDIA NIM response must be a JSON object")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("NVIDIA NIM response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, (str, dict)):
            raise LLMResponseError("NVIDIA NIM response has no usable content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return RawCompletion(content=content, provider=self.name, model=self.model, usage=usage)


def provider_for(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "gateway":
        return GatewayLLMProvider(settings)
    if settings.llm_provider == "bedrock":
        return BedrockLLMProvider(settings)
    if settings.llm_provider == "vertex_maas":
        return VertexMaaSLLMProvider(settings)
    if settings.llm_provider == "nvidia_nim":
        return NvidiaNIMLLMProvider(settings)
    if settings.llm_provider == "fake":
        return FakeLLMProvider()
    return DisabledLLMProvider()


def llm_client(settings: Settings | None = None, provider: LLMProvider | None = None) -> LLMClient:
    resolved = settings or get_settings()
    return LLMClient(provider or provider_for(resolved), resolved)
