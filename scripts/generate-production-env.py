import argparse
import base64
import secrets


def token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SponsorFlow production bootstrap secrets")
    parser.add_argument("domain", help="public DNS name, for example sponsorflow.example.com")
    args = parser.parse_args()
    domain = args.domain.strip().lower()
    if not domain or "://" in domain or "/" in domain:
        parser.error("domain must be a bare DNS name")

    print(f"SPONSORFLOW_DOMAIN={domain}")
    print(f"POSTGRES_PASSWORD={token()}")
    print(f"SPONSORFLOW_ADMIN_API_KEY={token()}")
    print(f"SPONSORFLOW_INBOUND_WEBHOOK_TOKEN={token()}")
    print(
        "SPONSORFLOW_PROVIDER_ENCRYPTION_KEY="
        + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    print(f"SPONSORFLOW_WEB_ADMIN_PASSWORD={token(18)}")
    print(f"SPONSORFLOW_WEB_SESSION_SECRET={token()}")


if __name__ == "__main__":
    main()
