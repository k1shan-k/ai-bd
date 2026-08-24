import asyncio

import pytest
from app.config import Settings
from app.llm import (
    BedrockLLMProvider,
    FakeLLMProvider,
    GatewayLLMProvider,
    LLMClient,
    LLMConfigurationError,
    LLMResponseError,
    LLMUnavailableError,
    OutreachDraft,
    VertexMaaSLLMProvider,
    provider_for,
)


def settings(**updates):
    return Settings(_env_file=None, environment="test", **updates)


def valid_outreach():
    return {
        "subject": "Summit sponsorship",
        "body": "A concise, grounded sponsorship note.",
        "personalization_fact_ids": [],
        "confidence": 0.9,
        "requires_human_review": False,
        "review_reasons": [],
    }


def test_structured_client_retries_malformed_json_then_validates():
    provider = FakeLLMProvider(responses={"outreach": ["not-json", valid_outreach()]})
    client = LLMClient(provider, settings(llm_max_retries=1))
    result = asyncio.run(
        client.generate(
            operation="outreach",
            system="system",
            prompt="prompt",
            output_model=OutreachDraft,
            temperature=0,
        )
    )
    assert result.value.subject == "Summit sponsorship"
    assert result.attempts == 2
    assert len(provider.calls) == 2
    assert len(result.prompt_hash) == 64


def test_structured_client_exhausts_malformed_output_without_partial_value():
    provider = FakeLLMProvider(responses={"outreach": ["bad", "still bad"]})
    client = LLMClient(provider, settings(llm_max_retries=1))
    with pytest.raises(LLMResponseError):
        asyncio.run(
            client.generate(
                operation="outreach",
                system="system",
                prompt="prompt",
                output_model=OutreachDraft,
                temperature=0,
            )
        )
    assert len(provider.calls) == 2


def test_structured_client_normalizes_unknown_provider_exception():
    class BrokenProvider:
        name = "broken"
        model = "broken-v1"

        async def complete(self, **kwargs):
            raise ConnectionError("secret upstream detail")

    client = LLMClient(BrokenProvider(), settings(llm_max_retries=0))
    with pytest.raises(LLMUnavailableError, match="broken failed after 1 attempt"):
        asyncio.run(
            client.generate(
                operation="outreach",
                system="system",
                prompt="prompt",
                output_model=OutreachDraft,
                temperature=0,
            )
        )


def test_gateway_openai_protocol_sends_runtime_key_and_parses_usage(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": valid_outreach()}}],
                "usage": {"input_tokens": 12, "output_tokens": 8},
            }

    class Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", Client)
    provider = GatewayLLMProvider(
        settings(
            llm_provider="gateway",
            llm_model="claude-test",
            llm_gateway_url="http://127.0.0.1:18080/",
            llm_gateway_path="/v1/chat/completions",
            llm_api_key="runtime-test-key",
        )
    )
    result = asyncio.run(
        provider.complete(
            operation="outreach",
            system="system",
            prompt="prompt",
            schema={},
            temperature=0.2,
            max_tokens=200,
        )
    )
    assert captured["url"] == "http://127.0.0.1:18080/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer runtime-test-key"
    assert captured["payload"]["model"] == "claude-test"
    assert captured["payload"]["stream"] is False
    assert result.content == valid_outreach()
    assert result.usage["output_tokens"] == 8


def test_bedrock_converse_adapter_parses_structured_text(monkeypatch):
    captured = {}

    class BedrockClient:
        def converse(self, **kwargs):
            captured.update(kwargs)
            return {
                "output": {"message": {"content": [{"text": '{"ok":true}'}]}},
                "usage": {"inputTokens": 3, "outputTokens": 2},
            }

    monkeypatch.setattr("app.llm.boto3.client", lambda *args, **kwargs: BedrockClient())
    provider = BedrockLLMProvider(
        settings(llm_provider="bedrock", llm_model="anthropic.claude-test")
    )
    result = asyncio.run(
        provider.complete(
            operation="test",
            system="system",
            prompt="prompt",
            schema={},
            temperature=0.1,
            max_tokens=100,
        )
    )
    assert captured["modelId"] == "anthropic.claude-test"
    assert captured["inferenceConfig"]["maxTokens"] == 100
    assert result.content == '{"ok":true}'
    assert result.usage["outputTokens"] == 2


def test_oversized_prompt_is_rejected_before_provider_call():
    provider = FakeLLMProvider()
    client = LLMClient(provider, settings(llm_max_input_chars=1_000))
    with pytest.raises(LLMResponseError, match="input exceeds"):
        asyncio.run(
            client.generate(
                operation="outreach",
                system="s" * 600,
                prompt="p" * 600,
                output_model=OutreachDraft,
                temperature=0,
            )
        )
    assert provider.calls == []


def test_vertex_maas_sends_unary_user_message_and_parses_usage(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": valid_outreach()}}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 9},
            }

    class Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", Client)
    provider = VertexMaaSLLMProvider(
        settings(
            llm_provider="vertex_maas",
            llm_model="meta/llama-4-maverick-17b-128e-instruct-maas",
            llm_vertex_project_id="example-project",
            llm_vertex_region="us-east5",
            llm_vertex_endpoint="us-east5-aiplatform.googleapis.com",
            llm_vertex_api_version="v1beta1",
            llm_vertex_auth_mode="access_token",
            llm_vertex_access_token="vertex-test-token",
        )
    )
    result = asyncio.run(
        provider.complete(
            operation="outreach",
            system="authoritative policy",
            prompt="structured prompt",
            schema={},
            temperature=0.2,
            max_tokens=200,
        )
    )
    assert captured["url"] == (
        "https://us-east5-aiplatform.googleapis.com/v1beta1/projects/example-project/"
        "locations/us-east5/endpoints/openapi/chat/completions"
    )
    assert captured["headers"]["Authorization"] == "Bearer vertex-test-token"
    assert captured["payload"]["model"] == "meta/llama-4-maverick-17b-128e-instruct-maas"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["messages"][0]["role"] == "user"
    assert "authoritative policy" in captured["payload"]["messages"][0]["content"]
    assert "structured prompt" in captured["payload"]["messages"][0]["content"]
    assert result.provider == "vertex-maas"
    assert result.content == valid_outreach()
    assert result.usage["completion_tokens"] == 9


@pytest.mark.parametrize(
    ("auth_mode", "expected_arguments"),
    [
        ("gcloud", ("gcloud", "auth", "print-access-token", "--quiet")),
        (
            "adc",
            (
                "gcloud",
                "auth",
                "application-default",
                "print-access-token",
                "--quiet",
            ),
        ),
    ],
)
def test_vertex_local_token_is_cached_without_shell(monkeypatch, auth_mode, expected_arguments):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"cached-local-token\n", None

    async def create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr("app.llm.asyncio.create_subprocess_exec", create_subprocess_exec)
    provider = VertexMaaSLLMProvider(
        settings(
            llm_provider="vertex_maas",
            llm_model="meta/test-model",
            llm_vertex_project_id="example-project",
            llm_vertex_auth_mode=auth_mode,
        )
    )

    async def acquire_twice():
        return await provider._access_token(), await provider._access_token()

    first, second = asyncio.run(acquire_twice())
    assert first == second == "cached-local-token"
    assert len(calls) == 1
    assert calls[0][0] == expected_arguments


def test_vertex_metadata_token_uses_google_metadata_header_and_cache(monkeypatch):
    captured = {"gets": 0}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "metadata-test-token", "expires_in": 3_600}

    class Client:
        def __init__(self, *, timeout, trust_env):
            captured.update(timeout=timeout, trust_env=trust_env)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            captured.update(url=url, headers=headers)
            captured["gets"] += 1
            return Response()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", Client)
    provider = VertexMaaSLLMProvider(
        settings(
            llm_provider="vertex_maas",
            llm_model="meta/test-model",
            llm_vertex_project_id="example-project",
            llm_vertex_auth_mode="metadata",
        )
    )

    async def acquire_twice():
        return await provider._access_token(), await provider._access_token()

    first, second = asyncio.run(acquire_twice())
    assert first == second == "metadata-test-token"
    assert captured["gets"] == 1
    assert captured["trust_env"] is False
    assert captured["headers"] == {"Metadata-Flavor": "Google"}


def test_vertex_authorization_failure_is_safe(monkeypatch):
    class Response:
        status_code = 403

    class Client:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            return Response()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", Client)
    provider = VertexMaaSLLMProvider(
        settings(
            llm_provider="vertex_maas",
            llm_model="meta/test-model",
            llm_vertex_project_id="example-project",
            llm_vertex_auth_mode="access_token",
            llm_vertex_access_token="secret-token-not-in-error",
        )
    )
    with pytest.raises(LLMConfigurationError, match="authorization failed with HTTP 403") as exc:
        asyncio.run(
            provider.complete(
                operation="test",
                system="system",
                prompt="prompt",
                schema={},
                temperature=0,
                max_tokens=10,
            )
        )
    assert "secret-token-not-in-error" not in str(exc.value)


def test_provider_factory_selects_vertex_maas():
    provider = provider_for(
        settings(
            llm_provider="vertex_maas",
            llm_model="meta/test-model",
            llm_vertex_project_id="example-project",
            llm_vertex_auth_mode="access_token",
            llm_vertex_access_token="test-token",
        )
    )
    assert isinstance(provider, VertexMaaSLLMProvider)


def test_vertex_refreshes_cached_token_once_after_unauthorized(monkeypatch):
    token_calls = []
    posted_tokens = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {"choices": [{"message": {"content": {"ok": True}}}]}

    class Client:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            posted_tokens.append(headers["Authorization"])
            return Response(401 if len(posted_tokens) == 1 else 200)

    provider = VertexMaaSLLMProvider(
        settings(
            llm_provider="vertex_maas",
            llm_model="meta/test-model",
            llm_vertex_project_id="example-project",
            llm_vertex_auth_mode="gcloud",
        )
    )

    async def access_token(*, force_refresh=False):
        token_calls.append(force_refresh)
        return "new-token" if force_refresh else "old-token"

    monkeypatch.setattr(provider, "_access_token", access_token)
    monkeypatch.setattr("app.llm.httpx.AsyncClient", Client)
    result = asyncio.run(
        provider.complete(
            operation="test",
            system="system",
            prompt="prompt",
            schema={},
            temperature=0,
            max_tokens=10,
        )
    )
    assert token_calls == [False, True]
    assert posted_tokens == ["Bearer old-token", "Bearer new-token"]
    assert result.content == {"ok": True}


def test_vertex_rejects_non_https_endpoint():
    with pytest.raises(LLMConfigurationError, match="HTTPS origin"):
        VertexMaaSLLMProvider(
            settings(
                llm_provider="vertex_maas",
                llm_model="meta/test-model",
                llm_vertex_project_id="example-project",
                llm_vertex_endpoint="http://127.0.0.1:9999",
                llm_vertex_auth_mode="access_token",
                llm_vertex_access_token="test-token",
            )
        )


def test_nvidia_nim_sends_reasoning_request_and_uses_only_final_content(monkeypatch):
    from app.llm import NvidiaNIMLLMProvider

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "private model reasoning",
                            "content": valid_outreach(),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 17, "completion_tokens": 11},
            }

    class Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", Client)
    provider = NvidiaNIMLLMProvider(
        settings(
            llm_provider="nvidia_nim",
            llm_model="deepseek-ai/deepseek-v4-flash-0731",
            llm_nvidia_api_key="nvidia-test-key",
            llm_nvidia_top_p=0.95,
            llm_nvidia_thinking=True,
            llm_nvidia_reasoning_effort="high",
        )
    )
    result = asyncio.run(
        provider.complete(
            operation="outreach",
            system="authoritative policy",
            prompt="structured prompt",
            schema={},
            temperature=0.4,
            max_tokens=16_384,
        )
    )

    assert captured["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer nvidia-test-key"
    assert captured["payload"] == {
        "model": "deepseek-ai/deepseek-v4-flash-0731",
        "messages": [
            {"role": "system", "content": "authoritative policy"},
            {"role": "user", "content": "structured prompt"},
        ],
        "temperature": 0.4,
        "top_p": 0.95,
        "max_tokens": 16_384,
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": "high"},
        "stream": False,
    }
    assert result.provider == "nvidia-nim"
    assert result.content == valid_outreach()
    assert result.usage["completion_tokens"] == 11
    assert "private model reasoning" not in str(result)


def test_nvidia_nim_authorization_failure_is_safe(monkeypatch):
    from app.llm import NvidiaNIMLLMProvider

    class Response:
        status_code = 401

    class Client:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            return Response()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", Client)
    provider = NvidiaNIMLLMProvider(
        settings(
            llm_provider="nvidia_nim",
            llm_model="deepseek-ai/deepseek-v4-flash-0731",
            llm_nvidia_api_key="secret-nvidia-key-not-in-error",
        )
    )
    with pytest.raises(LLMConfigurationError, match="authorization failed with HTTP 401") as exc:
        asyncio.run(
            provider.complete(
                operation="test",
                system="system",
                prompt="prompt",
                schema={},
                temperature=0,
                max_tokens=128,
            )
        )
    assert "secret-nvidia-key-not-in-error" not in str(exc.value)


def test_nvidia_nim_requires_key_and_factory_selects_provider():
    from app.llm import NvidiaNIMLLMProvider

    with pytest.raises(LLMConfigurationError, match="llm_nvidia_api_key"):
        NvidiaNIMLLMProvider(
            settings(
                llm_provider="nvidia_nim",
                llm_model="deepseek-ai/deepseek-v4-flash-0731",
            )
        )

    provider = provider_for(
        settings(
            llm_provider="nvidia_nim",
            llm_model="deepseek-ai/deepseek-v4-flash-0731",
            llm_nvidia_api_key="nvidia-test-key",
        )
    )
    assert isinstance(provider, NvidiaNIMLLMProvider)
