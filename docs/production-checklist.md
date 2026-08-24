# Production go/no-go checklist

A single unchecked **NO-GO** item blocks external outreach.

## Release integrity

- [ ] Release commit/tag passed backend PostgreSQL tests, Ruff, frontend typecheck/build, deployment preflight, and secret scan.
- [ ] Working tree or extracted release matches the reviewed checksum.
- [ ] No `.env*`, database, backup, log, session, private-key, or provider credential exists in source control or the release archive.
- [ ] The event templates contain approved real event material rather than examples.

## Host and network

- [ ] New patched VM; no reuse of a previously compromised host.
- [ ] SSH key authentication, restricted operator source ranges, minimal administrators.
- [ ] Only TCP 22/80/443 and UDP 443 are allowed as required.
- [ ] Ports 3000, 5432, 8000, and 18080 fail from an external network.
- [ ] Encrypted disk, time synchronization, security updates, bounded container logs.
- [ ] HTTPS certificate is valid; HSTS/CSP/security headers are present.

## Secrets and recovery

- [ ] `.env.production` is mode `0600`, ignored by Git, and backed up in an approved secret manager.
- [ ] NVIDIA key is production-scoped, current, and verified by a no-send schema smoke.
- [ ] Provider-encryption key has a separate recoverable copy.
- [ ] PostgreSQL backup is copied to encrypted off-host storage and checksum verified.
- [ ] Isolated restore drill completed; restored workers stayed off until reconciliation.
- [ ] Rotation and incident owners are named.

## Application health

- [ ] Migration completed successfully; API, worker, CRM, PostgreSQL, and Caddy are healthy.
- [ ] API and database are not directly public.
- [ ] Admin login rejects invalid credentials and grants valid sessions only over HTTPS.
- [ ] Production reports `provider_mode=live`; fake/disabled providers cannot launch.
- [ ] Worker heartbeat and Telegram singleton/cursor health are visible.

## Connectors

- [ ] Tavily check passes and cited public-business research has been manually reviewed.
- [ ] SES sender/production access/configuration set/SNS receipt and event paths pass.
- [ ] Telegram dedicated account authenticates; reconnect/replay/cursor behavior passes.
- [ ] WhatsApp signed webhook and approved template mode pass with a test identity.
- [ ] Cal.com availability, booking, signed callback, reschedule, and cancellation pass.
- [ ] Provider callbacks use the exact production HTTPS origin.
- [ ] No check response or log contains a credential or personal message body.

## Policy and pilot

- [ ] Exact `yes`/`maybe` consent import reviewed; invalid/duplicate/suppressed/quarantined counts reconciled.
- [ ] Global suppression and opt-out cancel all pending cross-event work.
- [ ] Package floors, discount caps, perks, inventory, expiry, and escalation rules approved.
- [ ] LLM outputs remain schema-validated and cannot directly send, suppress, discount, reserve, or book.
- [ ] Internal recipients complete the full email/Telegram/WhatsApp/reply/offer/booking journey.
- [ ] Telegram new-contact quota remains `<= 20` per configured day.
- [ ] First external cohort size and human supervision owner are explicitly approved.
- [ ] Emergency action to stop the worker has been rehearsed.
