#!/usr/bin/env python3

import argparse
import getpass
import os
from pathlib import Path

SUPPORTED_SECRETS = {
    "SPONSORFLOW_LLM_NVIDIA_API_KEY": lambda value: (
        value.startswith("nvapi-") and not any(character.isspace() for character in value)
    ),
}


def update(path: Path, name: str, value: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("environment path must be an existing regular file, not a symlink")
    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{name}="
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {name} setting")
    lines[matches[0]] = prefix + value
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Set a SponsorFlow production secret safely")
    parser.add_argument("name", choices=sorted(SUPPORTED_SECRETS))
    parser.add_argument("--env-file", type=Path, default=Path(".env.production"))
    args = parser.parse_args()
    value = getpass.getpass(f"{args.name}: ")
    if not value or len(value) > 8192 or not SUPPORTED_SECRETS[args.name](value):
        raise SystemExit(f"invalid value for {args.name}")
    update(args.env_file.resolve(), args.name, value)
    print(f"Updated {args.name} in owner-only environment file")


if __name__ == "__main__":
    main()
