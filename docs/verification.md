# Acceptance and verification matrix

This matrix separates code/test evidence from checks that require the clean production host and authorized provider accounts.

| Criterion | Implementation evidence | Test evidence | Current status |
|---|---|---|---|
| Only exact yes/maybe enters outreach | normalized import gate and provenance | exact-answer/import tests | Verified in suite |
| Duplicate files/leads/messages are idempotent | claims, unique keys, outbox/provider IDs | replay and PostgreSQL concurrency tests | Verified in suite/CI |
| Ambiguous identities do not merge | cross-identifier quarantine | import fixtures | Verified in suite |
| Global suppression covers events/channels | shared contact lock and all-lead cancellation | two-event and PostgreSQL race tests | Verified in suite/CI |
| Telegram never exceeds 20 new contacts/day | hard config bound and locked daily ledger | workflow and threaded PostgreSQL tests | Verified in suite/CI |
| Local daytime/cutoff apply | timezone scheduler and send policy | DST/opening/closing/invalid-zone tests | Verified in suite |
| Reply/terminal/manual state cancels automation | lock fences and dispatch guards | workflow cancellation/reopen/takeover tests | Verified in suite |
| Context is immutable and pinned | hash/version/context IDs | context/pinning tests | Verified in suite |
| Research claims are cited | Tavily/CSV schema and exact-URL validation | fabricated/unsupported claim tests | Verified in suite; live Tavily quality review required |
| LLM is schema-bound and fail-closed | provider-neutral client, Pydantic outputs, bounded retry | malformed/unsafe/provider adapter tests | Verified; real NVIDIA no-send smoke passed |
| LLM cannot execute transactions | no provider/send/suppression/offer/calendar tools in LLM layer | AI workflow/policy tests | Verified in architecture and suite |
| Offer respects floor/caps/perks/inventory | deterministic validator and shared ledger | boundary, bypass, expiry, replacement, race tests | Verified in suite/CI |
| Call-ready prospects qualify/book | pinned rules and calendar adapter | slot/booking/conflict tests | Verified in suite; live Cal.com contract required |
| Callbacks are authentic/replay-safe | SNS/Meta/Cal HMAC and provider-event claims | signature/staleness/replay tests | Verified in suite; live callbacks required |
| Fake sends cannot become production truth | production rejects fake and readiness gates launch | config/workflow tests | Verified |
| Operator actions are attributable | API-key roles, web session, audit entities | RBAC/operations/auth tests | Verified; external IdP recommended for larger teams |
| Full CSV-to-call pilot works | CRM, workflows, adapters, fake simulation | deterministic AI end-to-end test | Verified in fake mode; internal live pilot required |
| Production source is credential-free | ignore rules, release scanner, archive builder | static secret scan | Verified statically; repeat on release artifact |
| Public topology exposes only HTTPS gateway | Compose edge/backend/egress networks | static Compose assertions/preflight | Verified statically; runtime external scan required |

## Commands verified on this development host

```bash
uv run ruff check backend scripts
PYTHONPATH=backend uv run pytest -q
cd frontend && npm run typecheck && npm run build
python3 scripts/production-preflight.py --env-file <synthetic-owner-only-env>
bash -n scripts/*.sh
```

Results: Ruff passed, 109 backend tests passed, frontend typecheck/build passed, and synthetic production preflight/static Compose isolation passed. A real NVIDIA NIM schema-validated `no_send=true` smoke passed on the first attempt. Docker runtime/image validation is unavailable on this development VM because no Docker-compatible CLI is installed.

## Required release and live gate

A release candidate is not approved for external outreach until:

1. Frozen `uv.lock` and `package-lock.json` installs pass in CI.
2. PostgreSQL-backed tests and migrations pass.
3. Release archive secret scan and checksum pass.
4. Production Compose images build and run on the clean host.
5. External scan confirms only 80/443 are public and HTTPS headers/certificate are valid.
6. Provider sandbox checks pass for NVIDIA, Tavily, SES, Telegram, WhatsApp, and Cal.com.
7. Signed callbacks and an internal-identities end-to-end run pass.
8. Backup/restore and outbox reconciliation drill passes.
9. Monitoring, retention, incident, rotation, and rollback owners are assigned.
10. Operators sign off context, packages, inventory, negotiation caps, cutoff, calendar, recipients, and supervised cohort size.

Use [Production checklist](production-checklist.md) as the binding go/no-go record.
