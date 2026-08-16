import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ContextVersion, Event, PackageInventory

REQUIRED_EVENT_DOCUMENTS = {
    "event.md",
    "audience.md",
    "packages.md",
    "negotiation-policy.md",
    "inventory.md",
    "faq.md",
    "qualification.md",
    "escalation.md",
}
REQUIRED_ORGANIZATION_DOCUMENTS = {"company.md", "voice-and-style.md"}


@dataclass(frozen=True)
class ParsedDocument:
    metadata: dict[str, Any]
    body: str


def parse_markdown_document(content: str) -> ParsedDocument:
    if not content.startswith("---\n"):
        return ParsedDocument(metadata={}, body=content.strip())
    marker = content.find("\n---\n", 4)
    if marker == -1:
        raise ValueError("front matter starts with --- but has no closing ---")
    raw = content[4:marker]
    metadata = yaml.safe_load(raw) or {}
    if not isinstance(metadata, dict):
        raise ValueError("front matter must be a mapping")
    return ParsedDocument(metadata=metadata, body=content[marker + 5 :].strip())


def _money(value: Any, field: str, errors: list[str]) -> Decimal:
    try:
        result = Decimal(str(value))
        if result < 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        errors.append(f"{field} must be a non-negative number")
        return Decimal("0")


def _positive_int(value: Any, field: str, errors: list[str], default: int) -> int:
    try:
        result = int(value)
        if result <= 0:
            raise ValueError
        return result
    except (ValueError, TypeError):
        errors.append(f"{field} must be a positive integer")
        return default


def compile_context(documents: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    names = set(documents)
    missing_event = REQUIRED_EVENT_DOCUMENTS - names
    missing_org = REQUIRED_ORGANIZATION_DOCUMENTS - names
    if missing_event:
        errors.append(f"missing event documents: {', '.join(sorted(missing_event))}")
    if missing_org:
        errors.append(f"missing organization documents: {', '.join(sorted(missing_org))}")

    parsed: dict[str, ParsedDocument] = {}
    for name, content in documents.items():
        try:
            parsed[name] = parse_markdown_document(content)
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(f"{name}: {exc}")

    package_meta = parsed.get("packages.md", ParsedDocument({}, "")).metadata
    policy_meta = parsed.get("negotiation-policy.md", ParsedDocument({}, "")).metadata
    inventory_meta = parsed.get("inventory.md", ParsedDocument({}, "")).metadata

    packages = package_meta.get("packages", [])
    if not isinstance(packages, list) or not packages:
        errors.append("packages.md must define a non-empty packages list")
        packages = []

    normalized_packages: list[dict[str, Any]] = []
    package_ids: set[str] = set()
    max_discount = _money(
        policy_meta.get("max_discount_percent"),
        "negotiation-policy.md max_discount_percent",
        errors,
    )
    if max_discount > 100:
        errors.append("max_discount_percent cannot exceed 100")

    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            errors.append(f"packages[{index}] must be a mapping")
            continue
        package_id = str(package.get("id", "")).strip()
        if not package_id or package_id in package_ids:
            errors.append(f"packages[{index}].id must be non-empty and unique")
            continue
        package_ids.add(package_id)
        list_price = _money(package.get("list_price"), f"packages[{index}].list_price", errors)
        min_price = _money(package.get("min_price"), f"packages[{index}].min_price", errors)
        if min_price > list_price:
            errors.append(f"package {package_id}: min_price cannot exceed list_price")
        floor_from_discount = list_price * (Decimal("1") - max_discount / Decimal("100"))
        if min_price < floor_from_discount:
            errors.append(
                f"package {package_id}: min_price permits a larger discount than policy maximum"
            )
        perks = package.get("perks", [])
        if not isinstance(perks, list) or any(not isinstance(perk, str) for perk in perks):
            errors.append(f"packages[{index}].perks must be a list of strings")
            perks = []
        normalized_packages.append(
            {
                "id": package_id,
                "name": str(package.get("name", package_id)),
                "list_price": str(list_price),
                "min_price": str(min_price),
                "perks": perks,
            }
        )

    currency = str(policy_meta.get("currency", "")).strip().upper()
    if len(currency) != 3:
        errors.append("negotiation-policy.md currency must be a 3-letter code")
    allowed_perks = policy_meta.get("allowed_custom_perks", [])
    forbidden_promises = policy_meta.get("forbidden_promises", [])
    escalation_rules = policy_meta.get("mandatory_escalation", [])
    for field, value in {
        "allowed_custom_perks": allowed_perks,
        "forbidden_promises": forbidden_promises,
        "mandatory_escalation": escalation_rules,
    }.items():
        if not isinstance(value, list):
            errors.append(f"negotiation-policy.md {field} must be a list")

    inventory = inventory_meta.get("inventory", {})
    if not isinstance(inventory, dict):
        errors.append("inventory.md inventory must be a mapping of package ID to quantity")
        inventory = {}
    for package_id, quantity in inventory.items():
        if package_id not in package_ids:
            errors.append(f"inventory references unknown package {package_id}")
        if not isinstance(quantity, int) or quantity < 0:
            errors.append(f"inventory quantity for {package_id} must be a non-negative integer")
    for package_id in package_ids - set(inventory):
        errors.append(f"inventory missing package {package_id}")

    offer_expiry_days = _positive_int(
        policy_meta.get("offer_expiry_days", 7),
        "negotiation-policy.md offer_expiry_days",
        errors,
        7,
    )
    event_metadata = parsed.get("event.md", ParsedDocument({}, "")).metadata
    if not str(event_metadata.get("name", "")).strip():
        errors.append("event.md must define a non-empty name")

    compiled = {
        "event": event_metadata,
        "organization": parsed.get("company.md", ParsedDocument({}, "")).metadata,
        "voice": parsed.get("voice-and-style.md", ParsedDocument({}, "")).metadata,
        "packages": normalized_packages,
        "negotiation": {
            "currency": currency,
            "max_discount_percent": str(max_discount),
            "allowed_custom_perks": allowed_perks if isinstance(allowed_perks, list) else [],
            "forbidden_promises": forbidden_promises
            if isinstance(forbidden_promises, list)
            else [],
            "mandatory_escalation": escalation_rules
            if isinstance(escalation_rules, list)
            else [],
            "offer_expiry_days": offer_expiry_days,
        },
        "inventory": inventory,
        "qualification": parsed.get("qualification.md", ParsedDocument({}, "")).metadata,
        "escalation": parsed.get("escalation.md", ParsedDocument({}, "")).metadata,
        "knowledge": {name: doc.body for name, doc in parsed.items()},
    }
    return compiled, errors


def activate_context(
    session: Session, event: Event, documents: dict[str, str], actor: str
) -> ContextVersion:
    session.refresh(event, with_for_update=True)
    compiled, errors = compile_context(documents)
    if errors:
        raise ValueError("; ".join(errors))
    canonical = json.dumps(documents, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()
    existing = session.scalar(
        select(ContextVersion).where(
            ContextVersion.event_id == event.id, ContextVersion.content_hash == content_hash
        )
    )
    if existing:
        return existing
    latest = session.scalar(
        select(func.max(ContextVersion.version)).where(ContextVersion.event_id == event.id)
    )
    version = ContextVersion(
        event_id=event.id,
        version=(latest or 0) + 1,
        content_hash=content_hash,
        documents=documents,
        compiled=compiled,
        validation_errors=[],
        created_by=actor,
    )
    session.add(version)
    session.flush()
    for package_id, quantity in compiled["inventory"].items():
        inventory = session.scalar(
            select(PackageInventory)
            .where(
                PackageInventory.event_id == event.id,
                PackageInventory.package_id == package_id,
            )
            .with_for_update()
        )
        if not inventory:
            candidate = PackageInventory(
                event_id=event.id,
                package_id=package_id,
                total_count=quantity,
            )
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
                inventory = candidate
            except IntegrityError:
                session.expire_all()
                inventory = session.scalar(
                    select(PackageInventory)
                    .where(
                        PackageInventory.event_id == event.id,
                        PackageInventory.package_id == package_id,
                    )
                    .with_for_update()
                )
        if not inventory:
            raise RuntimeError(f"failed to initialize inventory for {package_id}")
        if inventory.reserved_count > quantity:
            raise ValueError(
                f"inventory for {package_id} cannot be reduced below active reservations"
            )
        inventory.total_count = quantity
    session.flush()
    return version
