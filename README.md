# SponsorFlow

SponsorFlow is a policy-controlled sponsorship business-development platform for event registrants. It imports CSV/Luma-style registrant exports, admits only exact `yes`/`maybe` sponsorship responses, researches and personalizes each eligible lead, coordinates email → Telegram → WhatsApp follow-up, manages replies and offers in one CRM, and books calls.

The repository includes a fail-closed fake-provider simulation for local development and live adapters for Amazon SES, a Telegram personal account through Telethon, WhatsApp Business Cloud, Cal.com, and Tavily. Production refuses fake mode. Live credentials are encrypted in PostgreSQL and managed from the authenticated **Providers** page; the server encryption key and web bootstrap secrets remain outside the UI.

## Implemented behavior

- Exact yes/maybe CSV eligibility, mapping preview, row provenance, conflict quarantine, idempotent file claims, and cross-event contact deduplication.
- Global identity suppression across email, Telegram, and WhatsApp; every existing event relationship is stopped together.
- Immutable, validated Markdown context snapshots for event facts, audience, packages, inventory, FAQ, qualification, escalation, voice, and negotiation caps.
- Deterministic day-0 email + Telegram and day-2/5/10 follow-ups, with WhatsApp fallback on day 5 when available.
- Account-wide hard maximum of 20 newly contacted Telegram prospects per configured quota day. Configuration may lower this limit but cannot raise it.
- Contact-local daytime windows, event cutoffs, durable scheduled actions, transactional outbox, provider idempotency keys, and reply cancellation fences.
- Source-cited research abstraction with confidence thresholds and safe escalation.
- Unified inbound event normalization, replay claims, message history, delivery state, qualification, slot offering, and idempotent booking.
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
uv sync --extra dev
cd frontend && npm install && cd ..
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
uv run ruff check backend
uv run pytest --cov=app --cov-report=term-missing
cd frontend
npm run typecheck
npm run build
```

CI runs backend tests against PostgreSQL so row locking and quota tests do not silently degrade to SQLite behavior.

### Validation status in this sandbox

- Python lint and syntax compilation: passed after the provider-control, callback, heartbeat, and Telegram-cursor changes.
- Full backend/PostgreSQL tests: authored but not executable here because FastAPI/SQLAlchemy/provider dependencies are absent and this sandbox has integrations-only networking.
- Frontend typecheck/build: not executable here because Next/React/type packages are absent and the npm cache is empty.
- Migration/Compose runtime: not executable here because PostgreSQL binaries/images and a Docker Compose provider are unavailable.
- Live callback/provider validation: requires the operator's deployed accounts and internal test identities.

These are explicit release blockers, not successful test results. CI or the authorized production host must run every remaining gate before outreach.

## Documentation

- [Production deployment](docs/deployment.md)
- [Architecture and workflows](docs/architecture.md)
- [Provider validation and production wiring](docs/provider-validation.md)
- [Security and operating runbook](docs/security-and-operations.md)
- [Acceptance and verification matrix](docs/verification.md)
