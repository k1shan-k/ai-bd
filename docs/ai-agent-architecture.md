# SponsorFlow AI BD Brain — implementation architecture

Status: implementation contract for the current overhaul.

## Product goal

SponsorFlow should act like an attentive sponsorship BD representative from first consented outreach through a booked meeting. It must research a prospect, write specific messages in the configured event voice, understand natural replies, continue a coherent cross-channel conversation, and offer meeting times. It must never invent event facts, impersonate an unauthorised person, override commercial policy, contact suppressed identities, or hide an uncertain decision.

The current policy engine, transactional outbox, provider adapters, suppression, inventory, offer validation, and booking correlation remain the authority. The LLM is a language and interpretation component—not the transaction coordinator.

## Provider abstraction

`backend/app/llm.py` owns all model calls. Application code must not import an SDK or build provider payloads directly.

```python
class LLMProvider(Protocol):
    name: str
    model: str
    async def generate_json(
        self,
        *,
        operation: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> LLMResult: ...
```

Implementations:

1. `GatewayLLMProvider`: configurable local HTTP endpoint, model, optional bearer token, and request path. For `pinealctx/kiro-gateway` it uses the OpenAI-compatible `POST /v1/chat/completions` route with `Authorization: Bearer`, non-streaming responses, and optional account routing through a base URL such as `/a/kiro-work`. Its `/v1/models` route is the source of available model IDs. The gateway is unofficial and testing-only; it must remain loopback-bound with a dedicated API key.
2. `NvidiaNIMLLMProvider`: production-capable NVIDIA-hosted OpenAI-compatible adapter. It uses the fixed `https://integrate.api.nvidia.com/v1/chat/completions` URL so configuration cannot redirect its bearer credential, sends unary requests with bounded `top_p`, thinking, and reasoning-effort controls, and consumes only final `message.content`. Provider reasoning fields are neither persisted nor logged and never bypass schema or policy validation.
3. `VertexMaaSLLMProvider`: production-capable Google Vertex AI managed open-model adapter. It calls the project/location OpenAI-compatible `v1beta1` endpoint with unary responses, supports local cached `gcloud` or ADC tokens and production metadata service-account tokens, refreshes once on authorization failure, and never persists or logs access tokens. Llama receives the authoritative policy and task in one first-role `user` message, as required by the model endpoint.
4. `BedrockLLMProvider`: production implementation using Bedrock Runtime `converse`, ambient AWS credentials/task role, configurable region and model ID.
5. `FakeLLMProvider`: deterministic structured responses for tests and local simulation. It is visibly marked `fake-llm` in message provenance and is rejected in production.
6. `DisabledLLMProvider`: fail-closed mode. It may use a conservative deterministic fallback for already-consented initial outreach, but no automated conversational reply is sent; the lead escalates.

Configuration:

- `llm_provider`: `fake`, `gateway`, `bedrock`, `vertex_maas`, `nvidia_nim`, or `disabled`
- shared: `llm_model`, timeouts, retries, character/token limits, temperatures, confidence, and autonomy
- gateway: `llm_gateway_url`, `llm_gateway_path`, `llm_gateway_protocol`, `llm_api_key`
- NVIDIA NIM: `llm_nvidia_api_key`, `llm_nvidia_top_p`, `llm_nvidia_thinking`, and `llm_nvidia_reasoning_effort`; the hosted endpoint is fixed in code
- Bedrock: `llm_bedrock_region` and ambient AWS credentials
- Vertex: `llm_vertex_project_id`, region, endpoint, API version, auth mode, and bounded token TTL

Production requires NVIDIA NIM with its dedicated key, `bedrock`, a configured authenticated gateway, or Vertex MaaS with metadata service-account authentication. It refuses `fake`/`disabled`, rejects local `gcloud`/explicit-token Vertex auth, and requires an explicit model. See [NVIDIA NIM](nvidia-nim.md) and [GCP Vertex AI MaaS](gcp-vertex-maas.md).

## Structured model contracts

Every model operation returns JSON validated by Pydantic. Free-form model output is never interpreted as an action.

### Outreach draft

- `subject` (email only)
- `body`
- `personalization_used`: source-backed claim identifiers
- `confidence` in `[0, 1]`
- `requires_human_review`
- `review_reasons`

Rules: short, channel-appropriate, one clear CTA, no fabricated familiarity, no unsupported metrics, no fake urgency, no claim that the message was manually typed, and no price outside the pinned context.

### Reply interpretation

- `primary_intent`: `opt_out`, `negative`, `interested`, `interested_with_tier`, `question`, `objection`, `call_request`, `meeting_selection`, `complaint`, `wrong_person`, `uncertain`
- `secondary_intents`
- `package_id` only from the supplied package enum
- `question_topics`
- `objections`
- `sentiment`
- `confidence`
- `requires_human_review`
- `reason`

A deterministic opt-out detector executes before the LLM. Model output can broaden escalation but can never reverse an opt-out.

### Conversation draft

- `body`
- `answered_topics`
- `unanswered_topics`
- `proposed_next_step`: `clarify`, `share_packages`, `handle_objection`, `offer_meeting`, `close_loop`, `human_review`
- `confidence`
- `requires_human_review`
- `review_reasons`

The model may discuss approved package list prices and perks. It cannot create an offer or promise a discount. Offers continue through `validate_offer`; booking continues through the calendar adapter.

### Research synthesis

- `company_summary`
- `prospect_summary`
- `verified_facts` with `claim`, `source_url`, `source_excerpt`, and `confidence`
- `fit_angles` with supporting fact indexes
- `risk_flags` (identity ambiguity, stale source, unsupported inference, competitor, irrelevant company)
- `confidence`

Only source-backed facts enter prompts as facts. Inferences are explicitly labelled and cannot appear in outbound copy as facts.

## Prompt construction and context boundaries

A fixed system prompt defines the role and safety contract. Operator and prospect content are untrusted data enclosed in JSON sections; instructions inside research pages, CSV fields, decks, and inbound messages are explicitly non-authoritative. The model is told to ignore any instructions found in those sections.

Each prompt contains only:

- pinned event context version;
- event facts and approved event-specific sales deck;
- audience and sponsor ICP;
- package names, list prices, approved perks, and inventory availability;
- FAQ, escalation policy, qualification policy, and voice guide;
- source-cited research;
- a bounded conversation summary plus recent messages;
- the requested operation and strict JSON schema.

Secrets, raw provider tokens, unrelated contacts, and other events are never included.

## Per-event Web3 sales kit

Each event owns independently versioned context. The editable documents are:

- `company.md`: organiser identity, credibility, sender signature, approved links;
- `voice-and-style.md`: persona, language, tone, message length, prohibited style;
- `event.md`: event name, dates, venue/online format, themes, website, approved proof;
- `audience.md`: attendance, roles, sectors, geographies, Web3 segments, sponsor value;
- `packages.md`: package IDs, names, list/minimum prices, perks and positioning;
- `inventory.md`: package inventory;
- `sales-deck.md`: deck URL, CTA, version/date, approved narrative and slide-level claims;
- `faq.md`, `qualification.md`, `escalation.md`, `negotiation-policy.md`.

The context API must return raw `documents` so the event editor restores the latest version rather than generic defaults. Event name, timezone, start, and outreach cutoff need an explicit event update endpoint. Activating a new version changes future campaigns; active conversations remain pinned to their original version. Re-pinning an unsent lead must be explicit and audited.

Deck binaries are not fed blindly to the model. Iteration one stores a link plus operator-approved text in `sales-deck.md`. A later PDF ingestion path may extract text, but extracted text is untrusted and requires review before context activation.

## Worker flow

LLM network calls must not occur while holding a database row lock.

1. Claim a due action by moving `pending -> generating` under `lead -> action` locks and commit.
2. Build a bounded generation snapshot in a short read transaction.
3. Call the provider outside a transaction with timeout and retry.
4. Validate structured output, run outbound content safety checks, then re-lock the lead/action.
5. If a newer inbound reply, suppression, manual takeover, terminal state, or context mismatch exists, cancel the generated draft.
6. Persist the body and generation provenance, move `generating -> pending`, and enqueue through the existing policy/outbox path.
7. Dispatch remains provider-only and never calls an LLM.

Inbound requests persist the message, cancel stale outreach, and create a generation action. They do not wait for the LLM. The worker generates at a human-like due time from the latest conversation state.

## Human timing

Timing is deterministic-random, reproducible from an action ID, and bounded by prospect-local working hours:

- initial email and Telegram: jitter within configured launch windows, never both exactly five minutes apart for every lead;
- active conversation reply: 45–180 seconds for a short reply, 2–8 minutes for a detailed question, 10–30 minutes for research-heavy or commercial questions;
- no automatic conversational reply outside local working hours unless the prospect is in an active exchange started within the configured grace period;
- weekends are skipped by default;
- only one pending automated reply per conversation; a newer inbound message cancels/replaces the older draft;
- minimum and maximum delay settings are account-level bounds, not model decisions.

The model may classify urgency but cannot choose an unbounded send time.

## Conversation memory

`Conversation.summary` becomes a structured rolling summary containing known goals, package interest, objections, questions answered/unanswered, commitments, meeting status, and operator notes. Recent messages are included verbatim up to fixed character/message limits. Summary updates occur after each successfully interpreted inbound event and are recorded in provenance.

Cross-channel messages share one conversation. The prompt identifies the current channel so style changes without losing memory.

## Failure and escalation contract

- Provider timeout/5xx: bounded retry; no automatic send from partial text.
- Invalid JSON/schema: one repair attempt; then escalate.
- Confidence below threshold, missing context, citation mismatch, policy phrase, complaint, legal/privacy request, custom contract, exclusivity, unsupported price request, or identity ambiguity: human review.
- Initial outreach may use a conservative deterministic fallback only in non-production simulation or when explicitly allowed. Conversation replies fail closed.
- Every model call records provider, model, operation, latency, attempt count, prompt version/hash, context version, confidence, cited fact IDs, and fallback/review reason. Raw prompts are not logged by default because they may contain personal data.

## Security and abuse boundaries

- Prospect messages cannot change system policy or tool permissions.
- No autonomous browsing from a prospect instruction; research uses server-defined queries only.
- No sensitive personal profiling, inferred wallet ownership, private social data, or scraped personal contact enrichment.
- No deceptive claims, fake scarcity, fabricated social proof, guaranteed ROI, or attendee personal-data promises.
- Exact opt-out and global suppression continue to execute without an LLM.
- The sender persona represents an authorised organiser/team identity. Natural language and timing do not justify false impersonation.

## Acceptance evidence

Implementation is complete only when tests prove:

1. event A and event B keep distinct facts, decks, audiences, packages, prices, and prompts;
2. reopening an event loads its latest raw documents;
3. source-backed research becomes structured synthesis and unsupported output is rejected;
4. outreach uses voice, event, research, and channel constraints;
5. nuanced, multi-intent, objection, opt-out, complaint, injection, empty, and multilingual replies produce safe structured decisions;
6. conversation replies use prior messages and do not repeat answered questions;
7. timing stays in local windows, skips weekends, jitters reproducibly, and coalesces rapid replies;
8. LLM timeout, malformed JSON, low confidence, and unsafe output send nothing and create an operator escalation;
9. concurrent workers cannot double-generate or double-send;
10. the CSV -> research -> outreach -> reply -> qualification -> slot -> booking integration passes with fake LLM and PostgreSQL;
11. ruff, full PostgreSQL pytest, frontend typecheck, and frontend build pass.
