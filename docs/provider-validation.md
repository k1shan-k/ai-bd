# Provider validation and production wiring

Status reviewed: 2026-08-16. Live adapters are implemented but remain behind the deployment-only `SPONSORFLOW_PROVIDER_MODE=live` safety switch. Production rejects fake mode. Credentials are entered at `/providers`, encrypted with AES-256-GCM, redacted from API responses, and refreshed by revision.

## Implemented provider contracts

### Amazon SES v2

- Sends through SES v2 with verified sender, optional configuration set, tags, provider message ID, and retry/terminal/ambiguous classification.
- Uses a per-lead plus-addressed Reply-To value. The SNS endpoint verifies AWS signatures, requires an exact topic ARN, confirms subscriptions, handles delivery/bounce/complaint/rejection events monotonically, and suppresses contacts on complaints or permanent bounces.
- SES receipt-rule SNS notifications containing raw MIME are parsed with a 150 KiB bound, plain-text preference/HTML text fallback, sender normalization, and exact lead correlation from the recipient address.
- If larger messages or attachments must be retained, use an encrypted S3/Lambda normalization pipeline instead of direct SNS content. AWS documents the direct SNS receipt action and size constraint: [SES SNS receipt action](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-action-sns.html) and [receipt notification shape](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-notifications-examples.html).

### Telegram personal account (Telethon)

- Encrypted StringSession bootstrap, OTP/optional 2FA in the admin UI, exact imported-username matching, account/dialog-qualified IDs, flood-wait classification, and PostgreSQL advisory-lock singleton ownership are implemented.
- The connector persists a per-account/per-chat message cursor. On first connection it records a non-processing baseline; on each listener cycle it replays messages after the durable cursor through provider-event deduplication before continuing live updates. Cursor writes advance monotonically even for unsupported/no-username messages, preventing replay loops.
- SponsorFlow—not connector sleeps—enforces the hard account-wide maximum of 20 new Telegram identities per configured day.
- Remaining live gate: verify reconnect/gap behavior against the dedicated account and inspect cursor advancement during a controlled disconnect. Do not scale the connector beyond the advisory-lock owner.

- Library reference: [Telethon stable documentation](https://docs.telethon.dev/en/stable/).

### WhatsApp Business Cloud

- Signed GET verification/POST HMAC webhook, inbound text/button/list/caption normalization, status reconciliation, phone matching, and retry classification are implemented.
- Business-initiated fallback uses an approved template. `message_body` sends one body text variable containing SponsorFlow's composed message; `none` sends a static template. The operator must select the mode matching the approved template exactly.
- Free-form text is used only for non-fallback conversation replies; validate the customer-service window and actual approved template with a test identity before launch.
- Reference: [Meta WhatsApp Cloud API overview](https://developers.facebook.com/docs/whatsapp/cloud-api/).

### Cal.com API v2

- Cal.com is the selected calendar provider. Availability uses the API v2 `/slots` contract; booking uses `/bookings` with lead/idempotency metadata. The webhook verifies raw-body HMAC, handles created/confirmed/rescheduled/cancelled/rejected/completed/no-show events, links replacement booking IDs, and can recover an ambiguous local booking from embedded lead metadata.
- SponsorFlow prevents duplicate local bookings, but Cal.com metadata is not a provider-native idempotency guarantee. An ambiguous timeout must be reconciled before a retry.
- References: [available slots](https://cal.com/docs/api-reference/v2/slots/get-available-time-slots-for-an-event-type) and [API v2 reference](https://cal.com/docs/api-reference/v2/).

### Tavily research

- Tavily replaces Exa. Search uses `POST /search` with Bearer authentication, bounded result counts, no raw page content, URL/retrieval time/excerpt/relevance provenance, conservative claim confidence, and a cache key containing provider contract/base URL/search settings.
- The provider check calls the non-destructive usage endpoint; malformed/error responses fail safely. Retrieved text is treated as untrusted business-source data and never authorizes tools or commercial actions.
- Reference: [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search).

## Activation gate

Do not launch real outreach until all of the following are true:

1. Production is behind HTTPS and only the CRM/gateway is public.
2. Every enabled provider's non-destructive check is green.
3. SES topic pin/subscription and receipt rule, Meta webhook, and Cal.com webhook are verified against the displayed URLs.
4. Provider sandbox/contract tests and PostgreSQL migration/concurrency tests pass in a dependency-enabled environment.
5. An internal test identity completes email → reply, Telegram, WhatsApp fallback/session reply, research, negotiation policy, slots, booking, reschedule, and cancellation.
6. Ambiguous-send reconciliation, monitoring, backup, secret rotation, and rollback owners are assigned.
7. The active context pack, sender identity, approved WhatsApp template, calendar owner, test recipients, and first supervised cohort receive operator sign-off.

Provider documentation summarized here is paraphrased; consult the linked official references during live-account validation.
