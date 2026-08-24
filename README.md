# SponsorFlow

SponsorFlow is a policy-controlled sponsorship business-development platform for event registrants. It imports CSV/Luma-style registrant exports, admits only exact `yes`/`maybe` sponsorship responses, researches and personalizes each eligible lead, coordinates email → Telegram → WhatsApp follow-up, manages replies and offers in one CRM, and books calls.

The repository includes a fail-closed fake-provider simulation for local development and live adapters for Amazon SES, a Telegram personal account through Telethon, WhatsApp Business Cloud, Cal.com, and Tavily. Production refuses fake mode. Live credentials are encrypted in PostgreSQL and managed from the authenticated **Providers** page; the server encryption key and web bootstrap secrets remain outside the UI.

## Implemented behavior

- Exact yes/maybe CSV eligibility, mapping preview, row provenance, conflict quarantine, idempotent file claims, and cross-event contact deduplication.
- Global identity suppression across email, Telegram, and WhatsApp; every existing event relationship is stopped together.
- Immutable, validated Markdown context snapshots for event facts, audience, packages, inventory, FAQ, qualification, escalation, voice, and negotiation caps.
- Deterministic day-0 email + Telegram; a successful initial Telegram send schedules day-2/5/10 follow-ups, with WhatsApp fallback on day 5 when available.
- Account-wide hard maximum of 20 newly contacted Telegram prospects per configured quota day. Configuration may lower this limit but cannot raise it.
- Contact-local daytime windows, event cutoffs, durable scheduled actions, a transactional outbox, local correlation keys, reply cancellation fences, and explicit stops for ambiguous provider outcomes.
- Source-cited research abstraction with confidence thresholds and safe escalation.
- Unified inbound event normalization, replay claims, message history, delivery state, qualification, slot offering, and locally correlated booking.
- Deterministic offer validation for package, floor price, discount, perks, inventory, expiry, replacement, rejection, and selected winning offer.
- Operator CRM for context, imports, pipeline, research, conversations, schedules, offers, meetings, quota, suppression, providers, analytics, manual takeover, and audits.
- Admin/operator/viewer API-key roles in exposed environments and fresh raw-body HMAC protection for normalized provider callbacks.
- Fake-provider accelerated campaign simulation, live provider adapters/control plane, and PostgreSQL-oriented concurrency tests.

## Repository map

```text
backend/app/                 FastAPI application, policies, workflows, adapters
backend/tests/               Unit, API, workflow, policy, and PostgreSQL race tests
backend/alembic/             Database migration bootstrap
frontend/app/                Next.js CRM routes
frontend/lib/                Typed API helper
contexts/                    Editable organization/event Markdown templates
examples/registrants.csv     Example import
.github/workflows/ci.yml      PostgreSQL backend CI and frontend build CI
docs/                        Architecture, provider, security, and verification guides
```

## Local setup

Prerequisites: Python 3.11+, `uv`, Node.js 22+, npm, and optionally PostgreSQL 16.

```bash
cp .env.example .env
uv sync --frozen --extra dev
cd frontend && npm ci && cd ..
uv run alembic upgrade head
```

Start each process in a separate terminal:

```bash
make api       # FastAPI at http://localhost:8000
make worker    # one worker cycle; use app.worker without --once for a deployed service
make web       # CRM at http://localhost:3000
```

FastAPI documentation is available at `http://localhost:8000/docs`. The default SQLite database and fake adapters are intended for local exploration only. For PostgreSQL, set:

```bash
SPONSORFLOW_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DATABASE
```

Container deployment is defined in `docker-compose.yml` for local fake mode. A host-ready production stack with PostgreSQL, one migration job, private API/worker/CRM services, and a Caddy HTTPS gateway is in `docker-compose.production.yml`; see `docs/deployment.md`.

## First pilot walkthrough

1. Open the CRM and create an event.
2. Open its workspace, replace every context template with approved event information, validate it, and activate an immutable version.
3. Upload `examples/registrants.csv` or your mapped export. Verify eligible, ineligible, suppressed, invalid, duplicate, and quarantined totals.
4. Create and activate the fast campaign.
5. Use **Simulate** first. Four logical worker cycles exercise days 0, 2, 5, and 10 without external sends.
6. Inspect each lead’s research, messages, schedules, policy timeline, and CRM state.
7. Exercise replies through `POST /api/v1/inbound`, offers through the lead CRM, and call booking through the fake calendar.
8. Before live outreach, deploy behind HTTPS, sign in at `/providers`, connect and test every account, configure the displayed callback URLs, and complete the release gates in `docs/provider-validation.md`.

## Quality gates

```bash
uv run ruff check backend scripts
uv run pytest --cov=app --cov-report=term-missing
cd frontend
npm run typecheck
npm run build
cd ..
python3 scripts/check-release.py .
```

CI runs backend tests against PostgreSQL so row locking and quota tests do not silently degrade to SQLite behavior. It also uses frozen Python/Node lockfiles, validates production settings, checks shell syntax, and scans release source for high-confidence credentials.

### Validation status

Verified on this host:

- Full backend Ruff and 109-test suite pass.
- Frontend typecheck and production build pass.
- Alembic upgrade/check and the PostgreSQL concurrency gates passed during the implementation verification cycle.
- Deterministic AI journey passes from CSV import through event-scoped research, personalized outreach, cross-channel replies, structured memory, qualification, authoritative slots, booking, and confirmation.
- A real schema-validated no-send smoke through NVIDIA NIM and `deepseek-ai/deepseek-v4-flash-0731` passed on the first attempt; reasoning remained ignored and `no_send=true` was enforced.
- NVIDIA request shape, fixed endpoint, final-content-only parsing, usage, authorization failures, dedicated-key validation, and production configuration are covered.
- Production environment generation/preflight, owner-only secret handling, shell syntax, Compose network/port isolation, and release-secret scanning pass statically.
- The temporary public test stack is stopped; there are no SponsorFlow listeners or processes on this host.

Still required before **live external outreach**:

- Deploy the reviewed release to a new, clean VM with Docker Compose, DNS, firewall policy, encrypted storage, and HTTPS. This development host has no Docker-compatible CLI and is not the production target.
- Configure and validate authorized SES, Telegram, WhatsApp, Cal.com, and Tavily accounts and signed callbacks over the production HTTPS origin.
- Complete an encrypted off-host backup/restore drill and operational monitoring/alerting.
- Replace every example event document and sales deck with approved event-specific content.
- Complete `docs/production-checklist.md` with internal test identities before approving a supervised external cohort.

NVIDIA NIM is the selected hosted LLM path. Vertex AI MaaS, Bedrock, and the unofficial loopback-only Kiro Gateway remain replaceable alternatives. Deterministic code continues to control consent, opt-out, pricing, inventory, timing, quotas, delivery, and booking.

## Production release

```bash
python3 scripts/generate-production-env.py crm.example.com --output .env.production
python3 scripts/set-production-secret.py SPONSORFLOW_LLM_NVIDIA_API_KEY --env-file .env.production
python3 scripts/production-preflight.py --env-file .env.production --require-docker --check-dns
./scripts/deploy-production.sh .env.production
```

Use `scripts/build-release.sh` to create a credential-free source archive and checksum. See the deployment runbook before using any command on a live host.

## Documentation

- [Operator knowledge base](docs/operator-knowledge-base.md)
- [Production deployment](docs/deployment.md)
- [Production go/no-go checklist](docs/production-checklist.md)
- [Full technical reference](docs/technical-reference.md)
- [NVIDIA NIM](docs/nvidia-nim.md)
- [GCP Vertex AI MaaS](docs/gcp-vertex-maas.md)
- [Architecture and workflows](docs/architecture.md)
- [Provider validation and production wiring](docs/provider-validation.md)
- [Security and operating runbook](docs/security-and-operations.md)
- [Acceptance and verification matrix](docs/verification.md)
