import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brain import RESEARCH_PROMPT_VERSION, synthesize_research
from app.config import Settings, get_settings
from app.llm import LLMClient, llm_client
from app.models import Contact, ContextVersion, EventLead, ResearchReport, TimelineEvent


class ResearchProvider(Protocol):
    name: str

    async def research(
        self, contact: Contact, context: ContextVersion | None
    ) -> list[dict[str, Any]]: ...


class FakeResearchProvider:
    name = "fake"

    async def research(
        self, contact: Contact, context: ContextVersion | None
    ) -> list[dict[str, Any]]:
        company = contact.company_name or "their organization"
        role = contact.role or "event registrant"
        source = f"csv://contact/{contact.id}"
        retrieved_at = datetime.now(UTC).isoformat()
        return [
            {
                "source_id": "csv-role",
                "url": source,
                "title": f"CSV registration role: {role}",
                "excerpt": f"{contact.full_name} registered with role {role}.",
                "retrieved_at": retrieved_at,
                "relevance": 1.0,
                "confidence": 1.0,
            },
            {
                "source_id": "csv-company",
                "url": source,
                "title": f"CSV registration company: {company}",
                "excerpt": f"{contact.full_name} registered with company {company}.",
                "retrieved_at": retrieved_at,
                "relevance": 1.0,
                "confidence": 1.0,
            },
        ]


class TavilyResearchProvider:
    name = "tavily"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def research(
        self, contact: Contact, context: ContextVersion | None
    ) -> list[dict[str, Any]]:
        if not self.settings.tavily_api_key:
            raise ValueError("Tavily API key is not configured")
        company = contact.company_name or ""
        role = contact.role or ""
        event_name = ""
        audience = ""
        if context:
            event_name = str(context.compiled.get("event", {}).get("name") or "")
            audience = str(context.compiled.get("knowledge", {}).get("audience.md") or "")[:300]
        query = " ".join(
            part
            for part in [
                company,
                role,
                "company products partnerships recent business profile",
                event_name,
                audience,
            ]
            if part
        )
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self.settings.tavily_base_url.rstrip('/')}/search",
                    headers={
                        "Authorization": f"Bearer {self.settings.tavily_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": query,
                        "search_depth": self.settings.tavily_search_depth,
                        "max_results": self.settings.tavily_result_limit,
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ValueError(f"Tavily research failed safely: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Tavily research returned invalid JSON") from exc
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ValueError("Tavily research returned an invalid result schema")
        sources: list[dict[str, Any]] = []
        retrieved_at = datetime.now(UTC).isoformat()
        for index, item in enumerate(results):
            url = item.get("url")
            title = str(item.get("title") or "Public business source")
            excerpt = str(item.get("content") or item.get("raw_content") or "")[:1_500]
            if not url:
                continue
            relevance = min(1.0, max(0.0, float(item.get("score") or 0.0)))
            sources.append(
                {
                    "source_id": f"tavily-{index + 1}",
                    "url": str(url),
                    "title": title,
                    "excerpt": excerpt,
                    "retrieved_at": retrieved_at,
                    "relevance": relevance,
                    "confidence": min(0.9, 0.55 + relevance * 0.35),
                }
            )
        return sources


def provider_for(settings: Settings, provider_name: str | None = None) -> ResearchProvider:
    selected = provider_name or settings.research_provider
    if selected == "fake":
        return FakeResearchProvider()
    if selected == "tavily":
        return TavilyResearchProvider(settings)
    raise ValueError(f"unknown research provider: {selected}")


async def research_lead(
    session: Session,
    lead: EventLead,
    provider_name: str | None = None,
    settings: Settings | None = None,
    context: ContextVersion | None = None,
    brain: LLMClient | None = None,
) -> ResearchReport:
    settings = settings or get_settings()
    provider = provider_for(settings, provider_name)
    lead_id = lead.id
    contact = session.get(Contact, lead.contact_id)
    if not contact:
        raise ValueError("lead contact not found")
    if context is None and lead.context_version_id:
        context = session.get(ContextVersion, lead.context_version_id)
    context_key = context.content_hash if context else "no-event-context"
    cache_key = hashlib.sha256(
        (
            f"{provider.name}:{contact.email_normalized}:{contact.company_name}:"
            f"{contact.role}:{settings.tavily_result_limit}:"
            f"{settings.tavily_search_depth}:{settings.tavily_base_url}:"
            f"{settings.llm_provider}:{settings.llm_model}:{context_key}:"
            f"{RESEARCH_PROMPT_VERSION}"
        ).encode()
    ).hexdigest()
    existing = session.scalar(
        select(ResearchReport).where(
            ResearchReport.lead_id == lead_id, ResearchReport.cache_key == cache_key
        )
    )
    if existing:
        return existing
    # Keep immutable prompt inputs, but release any caller-held lead lock and DB transaction.
    session.commit()
    try:
        sources = await provider.research(contact, context)
        synthesis = await synthesize_research(
            brain or llm_client(settings),
            settings,
            contact=contact,
            context=context,
            sources=sources,
        )
    except Exception as exc:
        failed_lead = session.scalar(
            select(EventLead)
            .where(EventLead.id == lead_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if failed_lead:
            if failed_lead.state != "suppressed":
                failed_lead.state = "escalated"
            session.add(
                TimelineEvent(
                    lead_id=lead_id,
                    event_type="escalated",
                    data={
                        "reason": "research_generation_failed",
                        "provider": provider.name,
                        "error_type": type(exc).__name__,
                    },
                )
            )
            session.commit()
        else:
            session.rollback()
        raise ValueError(
            f"research generation failed safely: {type(exc).__name__}"
        ) from exc
    lead = session.scalar(
        select(EventLead)
        .where(EventLead.id == lead_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not lead:
        raise ValueError("lead disappeared while research was running")
    existing = session.scalar(
        select(ResearchReport).where(
            ResearchReport.lead_id == lead_id, ResearchReport.cache_key == cache_key
        )
    )
    if existing:
        return existing
    report = ResearchReport(
        lead_id=lead_id,
        provider=provider.name,
        summary=synthesis.summary,
        facts=synthesis.facts,
        fit_angles=synthesis.fit_angles,
        confidence=Decimal(str(synthesis.confidence)),
        cache_key=cache_key,
    )
    session.add(report)
    if (
        lead.state not in {"won", "lost", "unresponsive", "suppressed"}
        and lead.automation_status == "active"
    ):
        lead.state = "ready"
    session.flush()
    return report
