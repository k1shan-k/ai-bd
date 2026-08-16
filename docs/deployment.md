# Production deployment

The production Compose stack is intended for one authorized Linux VPS with Docker Compose, a public DNS name, and inbound TCP 80/443 plus UDP 443. Caddy obtains and renews TLS automatically. Only Caddy publishes host ports; FastAPI, PostgreSQL, the worker, and Next.js remain on the private Compose network.

## Bootstrap

1. Point the domain's A/AAAA record to the host and open 80/443.
2. Copy this repository to the host.
3. Generate the one-time bootstrap file and store it as a protected secret:

```bash
PYENV_VERSION=3.11.15 python scripts/generate-production-env.py sponsorflow.example.com > .env.production
chmod 600 .env.production
```

The generated file includes the first web-admin password. Record it in a password manager before distributing operational access. The provider encryption key must be backed up separately; losing it makes encrypted provider credentials unrecoverable.

4. Build, migrate, and start:

```bash
docker compose --env-file .env.production -f docker-compose.production.yml up -d --build
```

5. Verify `https://sponsorflow.example.com/login`, sign in, then open **Providers**. The migration service must complete successfully, API/CRM health checks must pass, and the API must not be reachable directly on port 8000.

## Provider callbacks

The Providers page displays the exact same-origin HTTPS callback URLs:

- SES delivery events and SNS receipt-rule email: `/api/webhooks/ses/events`
- WhatsApp Cloud: `/api/webhooks/whatsapp`
- Cal.com: `/api/webhooks/calcom`

The Next.js ingress proxy preserves raw request bodies and provider signature headers. FastAPI verifies SNS certificates/topic pinning, Meta HMAC, or Cal.com HMAC before accepting data. The management BFF is separate and requires the signed HttpOnly admin session.

For SES replies, configure a receipt rule for the Reply-To domain that publishes the complete received message to the pinned SNS topic. The configured mailbox/domain must support plus addressing because outbound Reply-To addresses include `+sponsorflow-<lead-id>` for exact correlation. SNS raw-message delivery must remain disabled because the endpoint expects an SNS envelope. SNS limits direct email content, so messages above the provider limit should use an S3/Lambda normalization path if required.

## Safe activation

1. Enter provider credentials only in `/providers`; do not send them in chat or commit them.
2. Save Telegram disabled, start its account login, enter the one-time code/2FA in the UI, and let successful authorization enable it.
3. For WhatsApp fallback, select `message_body` only if the approved template has exactly one body text variable; select `none` for a static approved template.
4. Run every non-destructive provider check.
5. Configure callbacks and confirm test callbacks reconcile in the CRM.
6. Use internal test identities for the first end-to-end run.
7. Confirm the Telegram daily ledger remains at or below 20 newly contacted identities.
8. Only then start a small supervised cohort.

Production startup refuses fake provider mode, and launch readiness fails when required live provider/check configuration is absent.

## Upgrade and rollback

Back up PostgreSQL and `.env.production` before upgrades. Apply migrations as a separate gate, inspect service health, then replace API/worker/CRM images. To stop external sends without deleting durable work, stop the worker first:

```bash
docker compose --env-file .env.production -f docker-compose.production.yml stop worker
```

Do not blindly restart `reconcile_required` outbox entries after a timeout or restore; compare provider-side IDs first. Roll back application images only to a version compatible with the applied database revision. Restore PostgreSQL to an isolated host for recovery drills.

## Current hosting boundary

These files make the application deployable but do not create a cloud account, VPS, DNS record, or repository authorization. An authorized hosting target is required before a public URL can be created. Live provider checks and the internal-identity pilot also require the operator's real provider accounts after deployment.
