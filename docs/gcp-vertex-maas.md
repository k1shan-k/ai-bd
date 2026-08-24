# GCP Vertex AI MaaS

SponsorFlow supports Vertex AI managed open models through the OpenAI-compatible unary chat-completions endpoint. Deterministic application code still controls consent, opt-out, pricing, inventory, timing, qualification, provider delivery, and booking; Vertex only produces schema-validated language and interpretation outputs.

## Model and endpoint

For Llama 4 Maverick in `us-east5`:

```text
SPONSORFLOW_LLM_PROVIDER=vertex_maas
SPONSORFLOW_LLM_MODEL=meta/llama-4-maverick-17b-128e-instruct-maas
SPONSORFLOW_LLM_VERTEX_PROJECT_ID=YOUR_PROJECT_ID
SPONSORFLOW_LLM_VERTEX_REGION=us-east5
SPONSORFLOW_LLM_VERTEX_ENDPOINT=us-east5-aiplatform.googleapis.com
SPONSORFLOW_LLM_VERTEX_API_VERSION=v1beta1
```

The provider constructs:

```text
https://ENDPOINT/v1beta1/projects/PROJECT_ID/locations/REGION/endpoints/openapi/chat/completions
```

Calls are unary (`stream=false`). Llama's endpoint requires the first message to have role `user`, so the provider places SponsorFlow's authoritative system policy and the schema-constrained task together in one user message. Responses must still pass the existing Pydantic contract before any workflow can use them.

## Local authentication

Use one of these modes only for local development:

```text
# Active gcloud CLI account:
SPONSORFLOW_LLM_VERTEX_AUTH_MODE=gcloud

# Application Default Credentials created by `gcloud auth application-default login`:
SPONSORFLOW_LLM_VERTEX_AUTH_MODE=adc
```

`gcloud` mode invokes `gcloud auth print-access-token --quiet`; `adc` mode invokes `gcloud auth application-default print-access-token --quiet`. Both commands run without a shell. The provider validates the result, caches it for a bounded period, never logs it, and refreshes once after HTTP 401/403. Do not paste a token into `.env` for normal development.

The token must include the `cloud-platform` OAuth scope. A CLI installed on a Compute Engine VM can still produce an insufficiently scoped token if the VM's access scopes are restricted. A user ADC login can provide an independently scoped local credential; production should instead use workload metadata. `ACCESS_TOKEN_SCOPE_INSUFFICIENT` must be fixed at the credential/VM layer; retrying or changing IAM alone does not fix it.

## Production authentication

Production configuration accepts only:

```text
SPONSORFLOW_LLM_VERTEX_AUTH_MODE=metadata
```

Run API and worker workloads with a dedicated service account and metadata/Application Default Credentials. For Compute Engine, GKE node-based credentials, or similar environments, ensure the workload token has the `cloud-platform` OAuth scope. Grant the runtime principal only the permissions it needs; MaaS prediction requires `aiplatform.endpoints.predict`, included in `roles/aiplatform.user`.

The provider calls the fixed Google metadata token endpoint with `Metadata-Flavor: Google`, bypasses environment proxies, caches the returned token only until shortly before expiry, and never persists it.

`access_token` mode exists only for controlled non-production tests with a short-lived token. Production settings reject `gcloud`, `adc`, and `access_token` modes.

## One-time project prerequisites

An authorized Google Cloud operator must:

1. Enable the Vertex AI API (`aiplatform.googleapis.com`).
2. Ensure organization policy allows the Cloud Commerce Consumer Procurement API when required.
3. Enable the selected open model in Model Garden and accept its EULA. SponsorFlow does not automate legal acceptance.
4. Hold the Consumer Procurement Entitlement Manager role when enabling the model.
5. Grant the runtime principal `aiplatform.endpoints.predict` (normally `roles/aiplatform.user`).
6. Review regional quota and billing for the selected model.

Do not grant entitlement-management permissions to the runtime service account merely to make predictions.

## Failure behavior

- HTTP 401/403: refresh once, then fail as an authorization/configuration error.
- Other non-rate-limited 4xx: fail as request configuration error.
- HTTP 429/5xx and network errors: bounded retry through `LLMClient`.
- Invalid JSON, missing choices, or schema-invalid output: fail closed and escalate through existing workflow policy.
- Provider delivery remains independently controlled by `SPONSORFLOW_PROVIDER_MODE`; selecting Vertex does not enable email, Telegram, or WhatsApp sends.
