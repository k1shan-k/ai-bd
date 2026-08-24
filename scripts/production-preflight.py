#!/usr/bin/env python3

import argparse
import base64
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
PLACEHOLDER = re.compile(r"(?:change[-_ ]?me|replace[-_ ]?me|your[-_ ]|example\.com)", re.I)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"invalid environment assignment at line {line_number}")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise SystemExit(f"invalid or duplicate environment name at line {line_number}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise SystemExit(f"invalid control character in {name}")
        values[name] = value
    return values


def require_secret(values: dict[str, str], name: str, minimum: int = 24) -> None:
    value = values.get(name, "")
    if len(value) < minimum or PLACEHOLDER.search(value):
        raise SystemExit(f"{name} is missing, too short, or still a placeholder")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate SponsorFlow production deployment inputs"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.production")
    parser.add_argument("--require-docker", action="store_true")
    parser.add_argument("--check-dns", action="store_true")
    args = parser.parse_args()
    path = args.env_file.resolve()
    if path.is_symlink() or not path.is_file():
        raise SystemExit("production environment must be a regular file, not a symlink")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"production environment must be owner-only, found mode {oct(mode)}")
    values = parse_env(path)

    for name in (
        "POSTGRES_PASSWORD",
        "SPONSORFLOW_ADMIN_API_KEY",
        "SPONSORFLOW_OPERATOR_API_KEY",
        "SPONSORFLOW_VIEWER_API_KEY",
        "SPONSORFLOW_INBOUND_WEBHOOK_TOKEN",
        "SPONSORFLOW_PROVIDER_ENCRYPTION_KEY",
        "SPONSORFLOW_WEB_ADMIN_PASSWORD",
        "SPONSORFLOW_WEB_SESSION_SECRET",
    ):
        require_secret(values, name)

    try:
        provider_key = base64.urlsafe_b64decode(
            values["SPONSORFLOW_PROVIDER_ENCRYPTION_KEY"]
            + "=" * (-len(values["SPONSORFLOW_PROVIDER_ENCRYPTION_KEY"]) % 4)
        )
    except Exception as exc:
        raise SystemExit("SPONSORFLOW_PROVIDER_ENCRYPTION_KEY is not valid base64") from exc
    if len(provider_key) != 32:
        raise SystemExit("SPONSORFLOW_PROVIDER_ENCRYPTION_KEY must decode to 32 bytes")

    domain = values.get("SPONSORFLOW_DOMAIN", "")
    if not domain or PLACEHOLDER.search(domain) or "://" in domain or "/" in domain:
        raise SystemExit("SPONSORFLOW_DOMAIN must be a real bare DNS name")
    llm_provider = values.get("SPONSORFLOW_LLM_PROVIDER", "")
    if llm_provider == "nvidia_nim":
        require_secret(values, "SPONSORFLOW_LLM_NVIDIA_API_KEY", minimum=30)
        if not values["SPONSORFLOW_LLM_NVIDIA_API_KEY"].startswith("nvapi-"):
            raise SystemExit("SPONSORFLOW_LLM_NVIDIA_API_KEY has an unexpected format")
    if not values.get("SPONSORFLOW_LLM_MODEL"):
        raise SystemExit("SPONSORFLOW_LLM_MODEL is required")

    settings_values = {
        name.removeprefix("SPONSORFLOW_").lower(): value
        for name, value in values.items()
        if name.startswith("SPONSORFLOW_")
    }
    settings_values.update(environment="production", provider_mode="live")
    try:
        from app.config import Settings

        Settings(_env_file=None, **settings_values)
    except (ImportError, ValidationError) as exc:
        raise SystemExit(f"production application settings are invalid: {exc}") from None

    ignored = subprocess.run(["git", "check-ignore", "--quiet", str(path)], cwd=ROOT, check=False)
    if (ROOT / ".git").exists() and ignored.returncode != 0:
        raise SystemExit("production environment is not excluded by .gitignore")

    if args.check_dns:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(domain, 443)}
        except socket.gaierror as exc:
            raise SystemExit(f"DNS does not resolve for {domain}: {exc}") from None
        if not addresses:
            raise SystemExit(f"DNS returned no addresses for {domain}")
        print(f"dns_addresses={len(addresses)}")

    docker = shutil.which("docker")
    if args.require_docker and not docker:
        raise SystemExit("docker is required")
    if docker:
        subprocess.run([docker, "compose", "version"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            [
                docker,
                "compose",
                "--env-file",
                str(path),
                "-f",
                str(ROOT / "docker-compose.production.yml"),
                "config",
                "--quiet",
            ],
            cwd=ROOT,
            check=True,
        )
        print("compose_render=valid")
    else:
        print("compose_render=skipped_no_docker")
    print(f"env_mode={oct(mode)}")
    print(f"domain={domain}")
    print(f"llm_provider={llm_provider}")
    print("production_preflight=passed")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
