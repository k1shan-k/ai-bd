# Security and operating runbook

## Authentication and roles

Set independent high-entropy keys for admin, operator, and viewer roles:

```text
SPONSORFLOW_ADMIN_API_KEY
SPONSORFLOW_OPERATOR_API_KEY
SPONSORFLOW_VIEWER_API_KEY
```

When any key is configured, caller-supplied role headers no longer grant access. The API derives the role from the matching key. In production, an admin key and callback HMAC secret are mandatory. Prefer placing the API behind your identity-aware gateway and exchanging authenticated session claims for short-lived internal credentials; do not place a long-lived management key in public browser JavaScript.

Role intent:

- Admin: provider/configuration and suppression removal, plus all operator actions.
- Operator: event, context, campaign, import, workflow, conversation, offer, and meeting operations.
- Viewer: read-only CRM and analytics.

## Provider ingress

The public same-origin routes under `/api/webhooks/*` proxy raw requests to the private API. FastAPI validates each provider's native authentication before parsing trusted metadata:

- AWS SNS certificate signature plus an exact configured topic ARN.
- Meta `X-Hub-Signature-256` HMAC using the stored app secret.
- Cal.com `X-Cal-Signature-256` raw-body HMAC using the stored webhook secret.

The normalized `/api/v1/inbound` and `/api/v1/inbound/delivery` endpoints remain available for an authorized internal normalization service. They require:

```text
X-Webhook-Timestamp: <unix seconds>
X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, timestamp + "." + raw_body)>
```

Do not expose FastAPI directly. Preserve raw callback bodies at the gateway, pin provider topics/accounts where supported, and do not log callback payloads or secrets.

## Secrets and data

Production recommendations:

- AWS Secrets Manager for provider credentials and API/HMAC keys.
- KMS encryption for database, S3, backups, and Telegram session material.
- Task roles for SES/S3 instead of static AWS credentials.
- TLS for every database, provider, and browser connection.
- Redact message bodies, email, phone, usernames, API keys, and raw provider payloads from infrastructure logs.
- Store raw MIME/import files in encrypted object storage with explicit retention.
- Keep only minimal suppression identities required to honor future opt-outs.
- Record access, export, deletion, and suppression changes in audit events.

## Daily operations

Before launching a campaign:

1. Confirm provider checks are green; a fake/disabled check must block a live launch.
2. Review the active immutable context hash and all commercial caps.
3. Confirm event date, outreach cutoff, timezone, package stock, and calendar owner.
4. Preview import outcomes and resolve quarantined identities manually.
5. Review suppression totals and the first several research reports/messages.
6. Run accelerated simulation.
7. Start with internal test identities, then a small supervised cohort.

Monitor:

- Pending/failed scheduled actions and outbox events.
- Telegram new-contact ledger (`<= 20`).
- Provider connector health, singleton ownership, durable Telegram cursors, and provider-event replay/deduplication.
- Reply cancellation latency.
- Escalations and manual-mode conversations.
- Bounce/complaint/opt-out events.
- Inventory reservations and offer expirations.
- Engagement, qualification, and booking rates.
- LLM/research latency, confidence, and cost after real providers are added.

## Incident actions

### Duplicate or unexpected send

1. Pause affected leads or campaign automation.
2. Disable the channel adapter/worker deployment if scope is uncertain.
3. Preserve outbox, provider ID, action, lead timeline, context hash, and logs.
4. Check idempotency key reconciliation and worker restart history.
5. Suppress affected identities when requested.
6. Do not resume until a replay test reproduces and fixes the failure.

### Telegram connector loss

1. Stop the worker or let the PostgreSQL advisory-lock owner exit; never start a second uncontrolled session owner.
2. Leave queued actions durable.
3. Re-authenticate through `/providers` if the session is invalid.
4. Inspect the per-account/per-chat cursor and provider-event deduplication; replay resumes after the last committed cursor.
5. Resume only after an inbound and outbound test identity succeeds.

### Incorrect offer or inventory

1. Pause the conversation and take over manually.
2. Preserve offer/context/policy timeline.
3. Correct a new Markdown context version; never edit historical snapshots.
4. Resolve active offer state and inventory under an audited admin operation.
5. Communicate corrections through the team account without inventing commitments.

### Opt-out or complaint

1. Confirm all identities for the contact appear in global suppression.
2. Confirm every event lead is stopped and pending work is cancelled.
3. Do not delete the minimal suppression key needed to prevent re-import outreach.
4. Escalate privacy/legal requests to the designated human owner.

## Backup and recovery

- Automated PostgreSQL backups with point-in-time recovery.
- Versioned encrypted S3 storage for imports/context snapshots/raw inbound artifacts.
- Quarterly restore drill into an isolated environment.
- After restore, keep live adapters disabled until outbox/provider reconciliation completes.
- Never blindly replay pending outbox records after a database restore; compare provider IDs and idempotency keys first.

## Retention defaults to decide before production

The application exposes the necessary records but does not impose organization-specific retention periods. Before pilot, document retention for CSV/raw rows, message bodies, research sources, raw provider events, audit records, meetings/offers, and suppression identities. Implement deletion jobs and legal holds according to the operating jurisdictions and organizational policy.
