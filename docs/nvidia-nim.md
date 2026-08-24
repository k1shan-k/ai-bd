# NVIDIA NIM

SponsorFlow supports NVIDIA's hosted OpenAI-compatible chat API through a dedicated provider. Deterministic application code still controls consent, suppression, prices, inventory, timing, qualification, delivery, and booking. Model output is only usable after JSON parsing, Pydantic validation, confidence checks, and the existing policy gates.

## Configuration

```text
SPONSORFLOW_LLM_PROVIDER=nvidia_nim
SPONSORFLOW_LLM_MODEL=deepseek-ai/deepseek-v4-flash-0731
SPONSORFLOW_LLM_NVIDIA_ENDPOINT=https://integrate.api.nvidia.com/v1/chat/completions
SPONSORFLOW_LLM_NVIDIA_API_KEY=REPLACEMENT_KEY
SPONSORFLOW_LLM_NVIDIA_TOP_P=0.95
SPONSORFLOW_LLM_NVIDIA_THINKING=true
SPONSORFLOW_LLM_NVIDIA_REASONING_EFFORT=high
SPONSORFLOW_LLM_MAX_OUTPUT_TOKENS=16384
```

The adapter calls the fixed endpoint:

```text
https://integrate.api.nvidia.com/v1/chat/completions
```

`SPONSORFLOW_LLM_NVIDIA_ENDPOINT` makes the non-secret destination explicit in generated and example environment files, but application validation accepts only the exact URL above. An accidental or malicious environment change therefore cannot redirect the NVIDIA bearer credential. The request is unary (`stream=false`). `chat_template_kwargs` is sent at the top level of the HTTP JSON body, matching how the OpenAI SDK merges `extra_body` into a request.

NVIDIA availability, trial status, terms, account permissions, quota, and model lifecycle remain external prerequisites. A configured model can still be rejected or unavailable for a particular NVIDIA account.

## Credential handling

Any API key pasted into chat, shell history, a ticket, or a log must be revoked and replaced. Do not reuse the key that was supplied during implementation.

For local testing, place a newly generated restricted/disposable key directly into the ignored `.env` file and keep the file owner-only:

```bash
chmod 600 .env
${EDITOR:-vi} .env
```

Edit only `SPONSORFLOW_LLM_NVIDIA_API_KEY=`. Do not send the replacement key through chat and do not pass it as a command-line argument. Editors can create swap or backup files; disable those features or use an appropriate secret injection mechanism. This host was previously compromised, so a clean host or disposable test credential is strongly preferred.

For production, inject the key from the deployment platform's secret manager rather than committing it, baking it into an image, or storing it in Compose YAML. Rotate it on suspected disclosure and after staff or automation access changes.

## Reasoning and structured output

The requested model settings enable thinking with high reasoning effort. SponsorFlow intentionally:

- sends the authoritative policy as a `system` message and the schema-constrained task as a `user` message;
- reads only `choices[0].message.content`;
- ignores `reasoning` and `reasoning_content` fields rather than printing, logging, or persisting them;
- applies task-specific temperatures from SponsorFlow instead of forcing the sample's `temperature=1`, because lower temperatures are safer for strict structured JSON;
- uses a bounded 16,384-token ceiling while output schemas retain much smaller field limits;
- fails closed if final content is absent, malformed, schema-invalid, low-confidence, or unsafe.

Reasoning never authorizes a send or transaction. The LLM cannot suppress contacts, discount, reserve inventory, send through a provider, or book a meeting.

## Failure behavior

- HTTP 401/403: safe authorization/configuration failure without the key or response body in the error.
- Other non-rate-limited 4xx: request configuration failure.
- HTTP 429/5xx and network errors: bounded retry through `LLMClient`.
- Invalid JSON, missing choices/content, or schema-invalid output: fail closed and escalate through existing workflow policy.
- `SPONSORFLOW_PROVIDER_MODE=fake` remains independent and prevents real provider delivery during testing.
