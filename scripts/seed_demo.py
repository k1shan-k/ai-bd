"""Seed a fake-provider pilot scenario so the CRM has data to inspect.

Run against a locally hosted API:
    PYTHONPATH=backend .venv/bin/python scripts/seed_demo.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("SEED_API_URL", "http://127.0.0.1:8000/api/v1")
KEY = os.environ.get("SPONSORFLOW_ADMIN_API_KEY", "")
ROOT = Path(__file__).resolve().parent.parent


def call(method: str, path: str, body: dict | None = None, files: tuple | None = None):
    url = f"{API}{path}"
    headers = {"x-actor": "seed-script"}
    if KEY:
        headers["x-api-key"] = KEY
    if files:
        boundary = "----sponsorflowseed"
        name, content = files
        payload = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        headers["content-type"] = f"multipart/form-data; boundary={boundary}"
        data = payload
    elif body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    else:
        data = None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def documents() -> dict[str, str]:
    org = ROOT / "contexts" / "organization"
    event = ROOT / "contexts" / "events" / "example-event"
    docs = {path.name: path.read_text() for path in org.glob("*.md")}
    docs.update({path.name: path.read_text() for path in event.glob("*.md")})
    return docs


def main() -> int:
    status, event = call(
        "POST",
        "/events",
        {"slug": "pilot-summit", "name": "Pilot Summit", "timezone": "UTC"},
    )
    if status != 201:
        print("event creation failed", status, event)
        return 1
    event_id = event["id"]
    print("event", event_id)

    status, context = call(
        "POST", f"/events/{event_id}/contexts/activate", {"documents": documents()}
    )
    print("context", status, context if status != 201 else context["id"])
    if status != 201:
        return 1

    status, campaign = call(
        "POST",
        f"/events/{event_id}/campaigns",
        {
            "name": "Pilot sequence",
            "context_version_id": context["id"],
            "followup_days": [2, 5, 10],
            "whatsapp_fallback_day": 5,
        },
    )
    print("campaign", status, campaign if status != 201 else campaign["id"])
    if status != 201:
        return 1
    print("activate", *call("POST", f"/campaigns/{campaign['id']}/activate"))

    csv_content = (ROOT / "examples" / "registrants.csv").read_bytes()
    status, imported = call(
        "POST", f"/events/{event_id}/imports", files=("registrants.csv", csv_content)
    )
    print("import", status, json.dumps(imported, indent=1) if status == 200 else imported)

    status, leads = call("GET", f"/leads?event_id={event_id}")
    print("leads", status, len(leads) if isinstance(leads, list) else leads)
    for lead in leads if isinstance(leads, list) else []:
        code, started = call(
            "POST", f"/leads/{lead['id']}/workflow/start", {"campaign_id": campaign["id"]}
        )
        print("  start", lead["full_name"], code, started if code != 200 else "ok")

    status, simulation = call("POST", f"/campaigns/{campaign['id']}/simulate", {})
    print("simulate", status, json.dumps(simulation, indent=1)[:1500] if status < 400 else simulation)
    print("event_id", event_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
