"""Post a signed inbound provider event to a locally hosted API.

The /inbound endpoints require fresh raw-body HMAC headers whenever an inbound webhook token
is configured, so manual testing needs the same signature the providers produce.

    PYTHONPATH=backend .venv/bin/python scripts/send_inbound.py '{"provider":"telegram", ...}'
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("SEED_API_URL", "http://127.0.0.1:8000/api/v1")
SECRET = os.environ.get("SPONSORFLOW_INBOUND_WEBHOOK_TOKEN", "")


def send(payload: dict, path: str = "/inbound") -> tuple[int, str]:
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = urllib.request.Request(
        f"{API}{path}",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-webhook-timestamp": timestamp,
            "x-webhook-signature": f"sha256={signature}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    target = sys.argv[2] if len(sys.argv) > 2 else "/inbound"
    status, text = send(json.loads(sys.argv[1]), target)
    print(status, text)
    raise SystemExit(0 if status < 400 else 1)
