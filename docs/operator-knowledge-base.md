# SponsorFlow operator knowledge base

This document is intentionally credential-free and safe to index. It is the operational quick-reference for SponsorFlow release, deployment, CRM use, provider integration, policy boundaries, incidents, and troubleshooting.

## What SponsorFlow does

SponsorFlow imports event registrants who explicitly answered `yes` or `maybe` to sponsorship interest, researches their public business context, drafts grounded outreach, coordinates email/Telegram/WhatsApp follow-up, interprets replies, manages constrained offers, and books calls. It is event-scoped, source-cited, policy-controlled, and designed to fail closed.

The LLM is a language component only. It cannot directly send messages, suppress contacts, set discounts, reserve inventory, change policy, or book meetings. Deterministic code controls consent, suppression, provider delivery, commercial limits, inventory, timing, quotas, and calendar actions.

## Production architecture

- Caddy: only public service; ports 80/443; automatic TLS and security headers.
- CRM: Next.js authenticated operator interface; private container port 3000.
- API: FastAPI management/workflow service; private port 8000.
- Worker: durable scheduled-action/outbox processing plus singleton Telegram listener.
- PostgreSQL: durable contacts, imports, contexts, leads, messages, offers, meetings, outbox, audits, encrypted provider credentials, cursors, and heartbeats.
- NVIDIA NIM: selected LLM provider using `deepseek-ai/deepseek-v4-flash-0731`.
- Tavily: public-business search provider; retrieved snippets remain untrusted source material.
- Live connectors: Amazon SES, Telegram via Telethon personal account, WhatsApp Business Cloud, and Cal.com.

Compose networks isolate PostgreSQL on an internal backend network. API/worker also use an un-published egress network for providers. CRM bridges backend to the edge network; Caddy sees only CRM.

## Main CRM workflow

1. Sign in over HTTPS.
2. Overview: create an event workspace.
3. Context pack: replace every example document; validate and activate an immutable version.
4. CSV import: upload Luma/registrant CSV. Required mapped concepts are name, email, Telegram, and sponsorship answer; company/role/timezone/WhatsApp are optional.
5. Pipeline: inspect eligible leads and quarantines.
6. Lead detail: run configured cited research, inspect claims/angles, view unified conversation and schedules, pause/take over/suppress, manage offers and meetings.
7. Campaign: create and activate, test internal identities first, then deliberately launch a small cohort.
8. Operations: review schedules, audit actions, suppression, quota, provider status, and analytics.

Only exact normalized `yes` or `maybe` sponsorship answers are eligible. Global identity suppression applies across channels and events.

## Context documents

Each event has immutable versions of:

- organization/company identity and authorized sender;
- voice and style;
- event facts, dates, venue/format, links, and approved proof;
- audience and sponsor value;
- packages, list/minimum prices, approved perks;
- inventory;
- sales-deck narrative and approved claims;
- FAQ, qualification, escalation, and negotiation policy.

Never use example content for live outreach. Existing conversations remain pinned to their activated context version.

## AI and research

Selected LLM configuration:

- provider `nvidia_nim`;
- model `deepseek-ai/deepseek-v4-flash-0731`;
- fixed NVIDIA HTTPS endpoint;
- unary output;
- thinking enabled with high reasoning effort;
- final `message.content` only; reasoning fields are ignored;
- strict JSON/Pydantic output contracts;
- bounded timeout, retries, input, and output;
- confidence and deterministic policy checks after generation.

Tavily searches public business information using company, role, and event relevance. It does not request raw pages or generated answers. Claims survive only when they reference an exact supplied source URL; unsupported facts and angles are discarded. Do not infer sensitive traits, wallet ownership, finances, private relationships, or personal behavior.

## Credential handling

Bootstrap secrets live outside source control in owner-only deployment configuration or an approved secret manager. Provider credentials entered at `/providers` travel only over HTTPS, are field-allowlisted, and are AES-256-GCM encrypted before PostgreSQL storage. The browser never receives stored secret values, only configured/not-configured booleans.

Telegram API ID and phone are non-secret config; API hash and final Telethon StringSession are encrypted. OTP and optional 2FA password are transient and not stored. The StringSession is equivalent to an authenticated account credential.

Runtime API/worker processes decrypt enabled provider records into memory. Host/Docker administrators can inspect process/container secrets; restrict privileged access. Losing the provider-encryption key makes encrypted connector credentials unrecoverable. A database plus that key permits decryption.

## Provider activation summary

### Tavily

Configure key, result count, and search depth. Check usage endpoint. Review citation quality before any draft use.

### Amazon SES

Prefer workload IAM credentials. Verify sender, production access, configuration set, event destination, exact SNS topic, receipt rule, plus-addressed reply domain, bounce/complaint suppression, and raw MIME size constraints.

### Telegram

Use a dedicated authorized account. Save API ID/phone/hash, request OTP, complete optional 2FA, and verify encrypted session. One advisory-lock owner listens. Durable account/chat cursors baseline old messages and replay only newer IDs. Hard limit: at most 20 newly contacted Telegram identities per configured quota day.

### WhatsApp

Configure Cloud API token, phone-number ID, graph version, app/verify secrets, approved template, language, and body-parameter mode. Verify GET challenge and POST HMAC callback. Business-initiated fallback must match the approved template exactly.

### Cal.com

Configure API key, event-type ID, API version, and webhook secret. Test slots, booking, signed callback, reschedule, cancellation, and ambiguous-timeout reconciliation.

## Deployment quick reference

```bash
python3 scripts/generate-production-env.py crm.example.com --output .env.production
python3 scripts/set-production-secret.py SPONSORFLOW_LLM_NVIDIA_API_KEY --env-file .env.production
python3 scripts/production-preflight.py --env-file .env.production --require-docker --check-dns
./scripts/deploy-production.sh .env.production
./scripts/production-status.sh .env.production
```

Only 80/443 should be publicly reachable. Complete `docs/production-checklist.md` before external outreach.

## Backup and upgrade

```bash
./scripts/production-backup.sh .env.production
```

The custom PostgreSQL dump is owner-only but not encrypted by the script; move it immediately to encrypted off-host storage. Keep provider-encryption/bootstrap secrets separately. Restore into isolation, leave the worker stopped, compare outbox/provider IDs, then enable processing only after reconciliation.

Before upgrades: stop worker if uncertain, back up, review migrations, deploy the reviewed commit, verify all health and callbacks, then resume a supervised cohort. Never use `docker compose down -v` in production.

## Emergency actions

Stop automated processing while preserving CRM/API/database:

```bash
docker compose --env-file .env.production -f docker-compose.production.yml stop worker
```

For duplicate/unexpected sends: stop worker, preserve outbox/provider IDs/timeline/logs, reconcile provider state, suppress on request, and do not replay ambiguous events blindly.

For connector compromise: revoke at provider, stop worker, replace credential through HTTPS UI, test, and inspect unauthorized provider activity. For host compromise: isolate VM, stop all provider credentials externally, preserve forensic evidence, rebuild a clean host, restore from known-good backup, and rotate every bootstrap/provider secret.

For opt-out/complaint: verify global suppression across every identity/event and cancellation of all pending work. Retain the minimum suppression identity needed to prevent future re-import contact.

## Troubleshooting

- Production settings reject startup: run `production-preflight.py`; verify owner-only env file and selected LLM key/model.
- HTTPS unavailable: verify DNS A/AAAA, firewall 80/443, Caddy logs, and no proxy/CDN mismatch.
- API/CRM unhealthy: run `production-status.sh`; inspect bounded Compose logs; verify migration exited zero.
- NVIDIA authorization failure: rotate/check account key and model entitlement; errors intentionally omit response bodies and keys.
- Research has no facts: confirm Tavily enabled/check green, company/role present, and returned URLs/excerpts support claims.
- Telegram not authorized: stop worker, reauthenticate in Providers, inspect singleton ownership and cursor progression.
- Actions stuck `reconcile_required`: compare provider-side IDs before any manual retry.
- Launch blocked: resolve every readiness error; production does not permit fake providers.

## Data governance

Sensitive records include imported identities/raw rows, messages, public-source excerpts, provider events, offers, meetings, audits, and encrypted credentials. Operators must define lawful basis, access control, retention, deletion/export, backup encryption, breach response, and jurisdiction. Do not use SponsorFlow for sensitive profiling or unauthorized contact enrichment.

## Canonical documents

- `docs/deployment.md`: clean-VM deployment and upgrades.
- `docs/production-checklist.md`: binding go/no-go gate.
- `docs/provider-validation.md`: connector contracts and live tests.
- `docs/security-and-operations.md`: roles, callbacks, incidents, retention.
- `docs/technical-reference.md`: detailed implementation reference.
- `docs/ai-agent-architecture.md`: LLM authority and safety boundaries.
- `docs/nvidia-nim.md`: selected model integration.
