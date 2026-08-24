#!/usr/bin/env python3

import argparse
import base64
import os
import re
import secrets
from pathlib import Path

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.??$"
)


def token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def validate_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_RE.fullmatch(domain):
        raise argparse.ArgumentTypeError("domain must be a valid bare DNS name")
    return domain


def render(domain: str) -> str:
    values = [
        ("COMPOSE_PROJECT_NAME", "sponsorflow"),
        ("SPONSORFLOW_DOMAIN", domain),
        ("POSTGRES_PASSWORD", token()),
        ("SPONSORFLOW_ADMIN_API_KEY", token()),
        ("SPONSORFLOW_OPERATOR_API_KEY", token()),
        ("SPONSORFLOW_VIEWER_API_KEY", token()),
        ("SPONSORFLOW_INBOUND_WEBHOOK_TOKEN", token()),
        (
            "SPONSORFLOW_PROVIDER_ENCRYPTION_KEY",
            base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        ),
        ("SPONSORFLOW_WEB_ADMIN_PASSWORD", token(24)),
        ("SPONSORFLOW_WEB_SESSION_SECRET", token()),
        ("SPONSORFLOW_LLM_PROVIDER", "nvidia_nim"),
        ("SPONSORFLOW_LLM_MODEL", "deepseek-ai/deepseek-v4-flash-0731"),
        (
            "SPONSORFLOW_LLM_NVIDIA_ENDPOINT",
            "https://integrate.api.nvidia.com/v1/chat/completions",
        ),
        ("SPONSORFLOW_LLM_NVIDIA_API_KEY", ""),
        ("SPONSORFLOW_LLM_NVIDIA_TOP_P", "0.95"),
        ("SPONSORFLOW_LLM_NVIDIA_THINKING", "true"),
        ("SPONSORFLOW_LLM_NVIDIA_REASONING_EFFORT", "high"),
        ("SPONSORFLOW_LLM_MAX_OUTPUT_TOKENS", "16384"),
        ("SPONSORFLOW_LLM_TIMEOUT_SECONDS", "120"),
        ("SPONSORFLOW_LLM_MAX_RETRIES", "2"),
        ("SPONSORFLOW_BACKUP_DIR", "./backups"),
    ]
    return "\n".join(f"{name}={value}" for name, value in values) + "\n"


def write_exclusive(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(path, 0o600)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate SponsorFlow production bootstrap secrets"
    )
    parser.add_argument("domain", type=validate_domain, help="public DNS name")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".env.production"),
        help="new owner-only environment file (default: .env.production)",
    )
    args = parser.parse_args()
    try:
        write_exclusive(args.output, render(args.domain))
    except FileExistsError:
        parser.error(f"refusing to overwrite existing file: {args.output}")
    print(f"Created owner-only production environment: {args.output}")
    print("NVIDIA key is intentionally blank; set it with scripts/set-production-secret.py")
    print("Store this file and its provider-encryption key in an approved secret manager.")


if __name__ == "__main__":
    main()
