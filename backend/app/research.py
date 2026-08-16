import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Contact, EventLead, ResearchReport


class ResearchProvider(Protocol):
    name: str

    def research(self, contact: Contact) -> dict[str, Any]: ...


class FakeResearchProvider:
    name = "fake"

    def research(self, contact: Contact) -> dict[str, Any]:
        company = contact.company_name or "their organization"
        role = contact.role or "event registrant"
        source = f"csv://contact/{contact.id}"
        return {
            "summary": f"{contact.full_name} is listed as {role} at {company}.",
            "facts": [
                {
                    "claim": f"Role: {role}",
                    "source_url": source,
                    "confidence": 1.0,
                },
                {
                    "claim": f"Company: {company}",
                    "source_url": source,
                    "confidence": 1.0,
                },
            ],
            "fit_angles": [
                f"Explore how {company} could reach the event audience.",
                "Connect sponsorship benefits to the prospect's stated event interest.",
            ],
            "confidence": Decimal("0.800"),
        }


class TavilyResearchProvider:
    name = "tavily"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def research(self, contact: Contact) -> dict[str, Any]:
        if not self.settings.tavily_api_key:
            raise ValueError("Tavily API key is not configured")
        company = contact.company_name or ""
        role = contact.role or ""
        query = " ".join(
            part
            for part in [
                company,
                role,
                "company products partnerships audience sponsorship business profile",
            ]
            if part
        )
        try:
            response = httpx.post(
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
                timeout=20.0,
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
        facts: list[dict[str, Any]] = []
        retrieved_at = datetime.now(UTC).isoformat()
        for item in results:
            url = item.get("url")
            title = str(item.get("title") or "Public business source")
            excerpt = str(item.get("content") or item.get("raw_content") or "")[:500]
            if not url:
                continue
            facts.append(
                {
                    "claim": title,
                    "source_url": url,
                    "excerpt": excerpt,
                    "retrieved_at": retrieved_at,
                    "relevance": min(1.0, max(0.0, float(item.get("score") or 0.0))),
                    "confidence": 0.65,
                }
            )
        if not facts:
            return {
                "summary": f"No reliable public business sources were found for {company or contact.full_name}.",
                "facts": [],
                "fit_angles": [],
                "confidence": Decimal("0.300"),
            }
        source_titles = "; ".join(fact["claim"] for fact in facts[:3])
        return {
            "summary": (
                f"Public business research for {company or contact.full_name} returned "
                f"{len(facts)} cited sources: {source_titles}."
            ),
            "facts": facts,
            "fit_angles": [
                f"Explore sponsorship relevance using the cited {facts[0]['claim']} source.",
                f"Connect {company or 'the organization'}'s public business focus to the event audience without making unsupported claims.",
            ],
            "confidence": Decimal("0.750"),
        }


def provider_for(settings: Settings, provider_name: str | None = None) -> ResearchProvider:
    selected = provider_name or settings.research_provider
    if selected == "fake":
        return FakeResearchProvider()
    if selected == "tavily":
        return TavilyResearchProvider(settings)
    raise ValueError(f"unknown research provider: {selected}")


def research_lead(
    session: Session,
    lead: EventLead,
    provider_name: str | None = None,
    settings: Settings | None = None,
) -> ResearchReport:
    settings = settings or get_settings()
    provider = provider_for(settings, provider_name)
    contact = session.get(Contact, lead.contact_id)
    if not contact:
        raise ValueError("lead contact not found")
    cache_key = hashlib.sha256(
        (
            f"{provider.name}:{contact.email_normalized}:{contact.company_name}:"
            f"{contact.role}:{settings.tavily_result_limit}:"
            f"{settings.tavily_search_depth}:{settings.tavily_base_url}:tavily-search-v1"
        ).encode()
    ).hexdigest()
    existing = session.scalar(
        select(ResearchReport).where(
            ResearchReport.lead_id == lead.id, ResearchReport.cache_key == cache_key
        )
    )
    if existing:
        return existing
    result = provider.research(contact)
    report = ResearchReport(
        lead_id=lead.id,
        provider=provider.name,
        summary=result["summary"],
        facts=result["facts"],
        fit_angles=result["fit_angles"],
        confidence=result["confidence"],
        cache_key=cache_key,
    )
    session.add(report)
    lead.state = "ready"
    session.flush()
    return report
