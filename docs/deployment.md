# Production deployment

This runbook deploys SponsorFlow to one clean Linux VM with Docker Compose, PostgreSQL, the API/worker/CRM, and Caddy-managed HTTPS. Production forces live provider mode. Only Caddy publishes host ports; PostgreSQL and FastAPI remain private Docker services.

## 1. Security boundary

Use a newly provisioned VM. Do not copy `.env`, `frontend/.env.local`, SQLite databases, provider sessions, logs, or credentials from a development or previously compromised host. Transfer source through Git or the verified release archive only.

Minimum host posture:

- Supported current Linux distribution with security updates enabled.
- Docker Engine and Docker Compose v2 installed from the vendor's official repository.
- A non-root deployment operator with Docker access; SSH keys only.
- DNS A/AAAA record for the production domain pointing to the VM.
- Inbound firewall: TCP 22 from approved operator addresses, TCP 80/443, and UDP 443. Do not open 3000, 5432, 8000, or 18080.
- Encrypted VM disk and an approved encrypted, off-host backup destination.
- At least 4 vCPU, 8 GiB RAM, 40 GiB disk for an initial pilot; adjust from observed build/database load.

Docker group membership is root-equivalent. Restrict it accordingly. Do not run unreviewed remote installation scripts or copy production credentials through chat.

## 2. Obtain a reviewed release

Git workflow:

```bash
git clone https://github.com/OWNER/REPOSITORY.git sponsorflow
cd sponsorflow
git switch --detach RELEASE_TAG_OR_COMMIT
git status --short
```

The status must be clean. Prefer a protected release tag or commit produced after CI and review. Alternatively, copy `sponsorflow-*.tar.gz` plus its `.sha256` file, verify with `sha256sum --check`, and extract it. The archive intentionally contains no Git history or credentials.

## 3. Generate bootstrap secrets

From the repository root:

```bash
python3 scripts/generate-production-env.py crm.example.com --output .env.production
```

The generator creates `.env.production` atomically with mode `0600`, refuses overwrite, and does not print secret values. It selects:

```text
SPONSORFLOW_LLM_PROVIDER=nvidia_nim
SPONSORFLOW_LLM_MODEL=deepseek-ai/deepseek-v4-flash-0731
SPONSORFLOW_LLM_NVIDIA_THINKING=true
SPONSORFLOW_LLM_NVIDIA_REASONING_EFFORT=high
SPONSORFLOW_LLM_MAX_OUTPUT_TOKENS=16384
SPONSORFLOW_LLM_TIMEOUT_SECONDS=120
```

The NVIDIA key is intentionally blank. Enter a newly issued production key through a hidden prompt:

```bash
python3 scripts/set-production-secret.py \
  SPONSORFLOW_LLM_NVIDIA_API_KEY \
  --env-file .env.production
```

Never commit `.env.production`. Store a separately encrypted recovery copy in the approved secret manager. Losing `SPONSORFLOW_PROVIDER_ENCRYPTION_KEY` makes database-stored connector credentials unrecoverable; possession of that key plus the database permits decryption.

The baseline single-VM deployment supplies bootstrap secrets as container environment variables, which Docker administrators can inspect. For higher assurance, adapt the deployment platform to inject them from a cloud secret manager or orchestrator secret store.

## 4. Preflight

Ensure DNS already resolves to the VM, then run:

```bash
python3 scripts/production-preflight.py \
  --env-file .env.production \
  --require-docker \
  --check-dns
```

Preflight validates file permissions, required secret lengths, provider-encryption key shape, production Pydantic settings, the NVIDIA key/model, Git ignore coverage, Docker Compose rendering, and DNS. It prints statuses but no secrets.

Review the rendered service model without printing environment values:

```bash
docker compose --env-file .env.production \
  -f docker-compose.production.yml config --services
```

Expected services are `postgres`, `migrate`, `api`, `worker`, `crm`, and `gateway`.

## 5. Deploy

```bash
./scripts/deploy-production.sh .env.production
```

The script reruns preflight, creates an owner-only PostgreSQL backup if an existing database service is present, builds from `uv.lock` and `package-lock.json`, applies Alembic migrations, waits for health checks, and verifies the public HTTPS login page.

Check status at any time:

```bash
./scripts/production-status.sh .env.production
```

The Compose topology is deliberately split:

- `backend` is an internal network for PostgreSQL/API/worker/CRM.
- `egress` gives API and worker outbound access to NVIDIA and live providers without publishing ports.
- `edge` connects only Caddy and CRM.
- API, worker, migration, and CRM containers run non-root, read-only, capability-dropped, and with bounded JSON logs.
- Caddy publishes only 80/443 and adds HSTS, CSP, framing, referrer, MIME, and permissions headers.

Verify from a different network:

```bash
curl --fail --head https://crm.example.com/login
```

Direct requests to `:3000`, `:5432`, and `:8000` must fail.

## 6. Configure live connectors

Only after HTTPS works, sign in at `/providers`. Connector credentials are encrypted with AES-256-GCM before database storage and are never returned to the browser. Non-secret identifiers remain ordinary database configuration.

Configure in this order:

1. Tavily search and a non-destructive usage check.
2. SES sender, reply domain, configuration set, SNS topic, receipt rule, and callback.
3. Telegram API ID/phone/API hash, then OTP and optional 2FA to create the encrypted StringSession.
4. WhatsApp Cloud identifiers, token/app/verify secrets, approved template, and signed callback.
5. Cal.com event type, API key/webhook secret, and signed callback.

Use workload identity/task roles instead of static AWS keys where possible. Telegram authorization is real even before a campaign launch; use a dedicated authorized account. Complete every gate in [Provider validation](provider-validation.md).

## 7. First live pilot

Production cannot switch to fake delivery. Use dedicated internal test recipients and actual sandbox/test-capable provider accounts:

1. Replace every example context document and sales deck with approved event-specific material.
2. Activate the immutable context version.
3. Import a CSV containing only internal test identities with exact `yes`/`maybe` consent.
4. Run configured Tavily research and review every cited fact.
5. Create the campaign, review generated drafts, and process a minimal supervised cohort.
6. Test replies, suppression, Telegram reconnect/cursor behavior, WhatsApp template delivery, slots, booking, reschedule, and cancellation.
7. Confirm Telegram's hard limit remains at or below 20 newly contacted identities per quota day.
8. Assign a human owner for escalations and ambiguous provider outcomes before adding external recipients.

The LLM never sends, suppresses, discounts, reserves inventory, or books. Deterministic application policy and provider adapters remain authoritative.

## 8. Backups and upgrades

Create a database backup:

```bash
./scripts/production-backup.sh .env.production
```

The resulting PostgreSQL custom-format dump is mode `0600` but is **not itself encrypted**. Move it immediately to encrypted off-host storage, retain its checksum, and run restore drills on an isolated host. Back up the provider-encryption/bootstrap secrets separately; never store the only copies beside the database backup.

Before an upgrade:

1. Stop the worker if send state is uncertain:
   ```bash
   docker compose --env-file .env.production -f docker-compose.production.yml stop worker
   ```
2. Back up PostgreSQL and secret-manager material.
3. Review migrations and release notes.
4. Deploy the reviewed commit with `scripts/deploy-production.sh`.
5. Verify status, provider checks, outbox state, and callbacks before resuming a cohort.

Do not blindly replay `reconcile_required` or pending outbox entries after timeout/restore. Compare provider-side IDs first. Application rollback is safe only when compatible with the applied database migration; prefer corrective forward migrations.

## 9. Emergency controls

Stop all automated outbound processing while keeping the CRM/API available:

```bash
docker compose --env-file .env.production \
  -f docker-compose.production.yml stop worker
```

Stop the full stack:

```bash
docker compose --env-file .env.production \
  -f docker-compose.production.yml down
```

Do not add `-v`; that would delete named volumes including PostgreSQL data.

Rotate a disclosed connector secret in its provider console, update it through `/providers`, test it, and invalidate the old credential. Rotate bootstrap secrets during a controlled maintenance window because changing the provider-encryption key requires decrypt/re-encrypt migration rather than simple replacement.

## 10. GitHub handoff

Before committing or pushing:

```bash
python3 scripts/check-release.py .
git status --short
git diff --check
```

On a dedicated release branch, stage explicit files, review `git diff --cached`, then commit and push the branch. Do not use `git add .`, do not push directly to `main`, and do not include `.env.production`, local databases, logs, backups, provider sessions, or release archives. Open a pull request and require CI/review before tagging a release.

## Known operator-supplied controls

This repository does not install Docker, create DNS/firewall rules, provision a VM, configure cloud IAM/KMS, automate PostgreSQL PITR, supply centralized monitoring/alerting, define jurisdiction-specific retention, or accept provider legal terms. Those remain authorized operator responsibilities.
