# Acceptance and verification matrix

This matrix distinguishes implemented evidence from checks that still require a dependency-enabled/PostgreSQL environment.

| Criterion | Implementation evidence | Test evidence | Current status |
|---|---|---|---|
| Only exact yes/maybe enters outreach | `importer.normalize_answer`, import gate | `test_sponsor_answer_requires_exact_yes_or_maybe` | Implemented; suite not run here |
| Duplicate files/leads/messages are idempotent | Savepoint claims, unique keys, outbox/provider IDs | import replay, provider replay, PostgreSQL concurrent import/provider tests | Implemented; PostgreSQL CI required |
| Ambiguous identities do not merge | email/Telegram conflict quarantine, WhatsApp support signal | import fixtures | Implemented; suite not run here |
| Global suppression covers all events/channels | contact identity ledger, shared contact lock, post-lock import recheck, all-lead lock/cancel/stop | two-event suppression and PostgreSQL suppression/import race tests | Implemented; PostgreSQL CI required |
| Telegram never exceeds 20 new contacts/day | setting hard max, locked daily ledger | 21-lead workflow test and 25-thread PostgreSQL test | Implemented; PostgreSQL CI required |
| Local daytime and cutoff apply | timezone scheduler plus enqueue/dispatch policy | New York opening, closing, DST, invalid-zone tests | Implemented; suite not run here |
| Reply/terminal state cancels later channels | lead→action→outbox lock fence plus independent terminal dispatch guard | reply replay, terminal cancellation, workflow replay/reopen tests | Implemented; PostgreSQL race CI recommended |
| Manual takeover blocks automation | lead-first cancellation and manual action | takeover and resume/outbox tests | Implemented; suite not run here |
| Context is immutable and pinned | event lock, hash/version constraints, lead context ID | version/idempotency/pinning tests | Implemented; suite not run here |
| Personalization claims are cited and used | Tavily/CSV research fact schema, URL/time/excerpt/relevance provenance, conservative confidence, first fit angle in initial email | fake research/composed-message tests; live Tavily evaluation pending | Implemented; live evaluation required |
| Offer cannot break floor/discount/perk/promise limits | deterministic offer validator with normalized forbidden-promise and mandatory-escalation matching | accepted/rejected boundary and punctuation/spacing bypass tests | Implemented; suite not run here |
| Inventory is shared across context versions | event/package ledger and row locks | idempotent offer/version exhaustion tests | Implemented; PostgreSQL CI required |
| Offer reservations settle correctly | replacement, expiry-at-selection, suppression, terminal/reopen, selected winner | idempotency, expired-winner, lost, reopen, selected-winner tests | Implemented; suite not run here |
| Call-ready prospects qualify/book | pinned qualification rules, Cal.com slots/bookings/lifecycle webhook, idempotent local meeting | call/slot test and disabled-policy test | Implemented; Cal.com sandbox contract pending |
| Callbacks are authentic and replay-safe | internal HMAC plus SNS certificate/topic pin, Meta HMAC, Cal.com HMAC, provider-event claim | internal signature/staleness/replay tests | Native adapters implemented; live callback tests pending |
| Fake sends cannot become production truth | production config rejects fake; campaign readiness requires configured live adapters | config tests | Implemented; suite not run here |
| Operator actions are attributable | private API-key roles, signed HttpOnly web session, route middleware, timeline/audit entities | RBAC/operations tests; frontend auth build pending | Implemented; external IdP recommended for larger teams |
| Full CSV-to-call pilot works | CRM + fake provider simulation | campaign simulation test | Implemented in fake mode; suite not run here |

## Commands attempted in the current sandbox

```text
PYENV_VERSION=3.11.15 python -m ruff check backend scripts
# Passed after final provider/cursor changes

PYENV_VERSION=3.11.15 python -m compileall -q backend scripts
# Passed after final provider/cursor changes

PYENV_VERSION=3.11.15 pytest -q
# Blocked at collection: ModuleNotFoundError: fastapi

npm run typecheck
# Blocked: Next/React/@types packages are not installed; npm cache is empty

docker compose --env-file <generated-env> -f docker-compose.production.yml config
# Blocked: no Docker Compose provider is installed
```

The sandbox is `INTEGRATIONS_ONLY`, so missing PyPI/npm dependencies cannot be downloaded. These blocked checks are not passes.

## Required release gate

A release candidate is not approved until a network-enabled environment or CI completes:

1. `uv sync --extra dev`.
2. PostgreSQL-backed `uv run pytest --cov=app --cov-report=term-missing`.
3. `uv run ruff check backend`.
4. `npm install`, `npm run typecheck`, and `npm run build`.
5. Migration up/down test on a production-like PostgreSQL snapshot.
6. Provider sandbox contract tests for every enabled adapter.
7. Internal-identities end-to-end run with verified callbacks.
8. Backup/restore and outbox reconciliation drill.
9. Security review of gateway authentication, provider signature adapters, secrets, logs, and retention.
10. Operator sign-off on context, packages, stock, negotiation caps, cutoff, and calendar.
