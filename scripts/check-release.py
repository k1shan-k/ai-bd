#!/usr/bin/env python3

import argparse
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "node_modules",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "backups",
    "release",
}
FORBIDDEN_NAMES = {
    ".env",
    ".env.production",
    ".env.local",
    "application_default_credentials.json",
    "credentials.json",
}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".pem", ".p12", ".pfx"}
PATTERNS = {
    "nvidia_api_key": re.compile(rb"nvapi-[A-Za-z0-9_-]{20,}"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github_token": re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "google_service_account": re.compile(rb'"type"\s*:\s*"service_account"'),
}


def candidates(root: Path) -> Iterable[Path]:
    if (root / ".git").is_dir():
        output = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
        )
        for raw in output.split(b"\0"):
            if raw:
                yield root / raw.decode()
        return
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not any(part in SKIP_DIRECTORIES for part in relative.parts):
            yield path


def findings(root: Path):
    for path in candidates(root):
        if path.is_symlink():
            target = path.resolve()
            try:
                target.relative_to(root)
            except ValueError:
                yield path, "symlink_outside_release"
            continue
        if not path.is_file():
            continue
        lower_name = path.name.lower()
        if lower_name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            yield path, "forbidden_file"
            continue
        try:
            data = path.read_bytes()
        except OSError:
            yield path, "unreadable_file"
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(data):
                yield path, name


def main() -> None:
    parser = argparse.ArgumentParser(description="Reject secrets and runtime data in a release tree")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    issues = [(str(path.relative_to(root)), kind) for path, kind in findings(root)]
    if issues:
        for path, kind in issues:
            print(f"release_safety_failure={kind} file={path}")
        raise SystemExit(1)
    print("release_secret_scan=passed")


if __name__ == "__main__":
    main()
