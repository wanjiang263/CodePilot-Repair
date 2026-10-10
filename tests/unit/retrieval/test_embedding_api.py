import json

import httpx
import numpy as np
import pytest

from codepilot.llm.config import ProviderSpec
from codepilot.llm.errors import LLMError, ProviderError, ProviderNotAllowedError
from codepilot.llm.retry import RetryPolicy
from codepilot.retrieval.documents import RetrievalError
from codepilot.retrieval.embeddings import (
    CachedEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)


def make_provider(handler, **options):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://wrong.test")
    spec = ProviderSpec(
        name="test",
        base_url="https://embedding.test/api/v1/",
        api_key_env="TEST_KEY",
        extra_headers={"X-Test": "value"},
    )
    return OpenAICompatibleEmbeddingProvider(
        provider=spec,
        model="configured-code-model",
        dimensions=2,
        revision="deploy-v1",
        api_key="test-secret",
        client=client,
        sleep=lambda _: None,
        **options,
    )


def test_batch_request_and_reordered_response():
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://embedding.test/api/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.headers["X-Test"] == "value"
        assert json.loads(request.content) == {
            "model": "configured-code-model",
            "input": ["def x(): pass", "中文"],
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={"data": [{"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]}]},
        )

    provider = make_provider(handler)
    try:
        np.testing.assert_array_equal(provider.embed(["def x(): pass", "中文"]), [[1, 2], [3, 4]])
        assert provider.embed([]).shape == (0, 2)
        assert len(requests) == 1
        with provider:
            pass
        assert not provider._client.is_closed  # Injected client ownership stays with caller.
    finally:
        provider._client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": []},
        {"data": [{"index": 0, "embedding": [1, 2]}, {"index": 0, "embedding": [3, 4]}]},
        {"data": [{"index": True, "embedding": [1, 2]}, {"index": 1, "embedding": [3, 4]}]},
        {"data": [{"index": 0, "embedding": [1, 2]}, {"index": 2, "embedding": [3, 4]}]},
        {"data": [None, None]},
        {"data": [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [2]}]},
        {"data": [{"index": 0, "embedding": [float("inf"), 1]}, {"index": 1, "embedding": [1, 2]}]},
    ],
)
def test_malformed_batch(payload):
    # httpx JSON serialization forbids inf; use raw JSON to exercise server behavior.
    provider = make_provider(lambda _: httpx.Response(200, content=json.dumps(payload)))
    try:
        with pytest.raises(RetrievalError):
            provider.embed(["x", "y"])
    finally:
        provider._client.close()


def test_invalid_json():
    provider = make_provider(lambda _: httpx.Response(200, content="not JSON"))
    try:
        with pytest.raises(RetrievalError):
            provider.embed(["x"])
    finally:
        provider._client.close()


@pytest.mark.parametrize("status, attempts", [(401, 1), (400, 1), (429, 3), (503, 3)])
def test_retry_uses_llm_policy(status, attempts):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"retry-after": "0"})

    provider = make_provider(handler, policy=RetryPolicy(max_attempts=3))
    try:
        with pytest.raises(LLMError) as error:
            provider.embed(["x"])
        assert error.value.status == status
        assert len(calls) == attempts
        assert "test-secret" not in str(error.value)
    finally:
        provider._client.close()


def test_transport_retry_then_success():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("failed", request=request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

    provider = make_provider(handler)
    try:
        assert provider.embed(["x"]).shape == (1, 2)
        assert len(calls) == 2
    finally:
        provider._client.close()


def test_transport_exhausted():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    provider = make_provider(handler, policy=RetryPolicy(max_attempts=1))
    try:
        with pytest.raises(ProviderError, match="transport"):
            provider.embed(["x"])
    finally:
        provider._client.close()


def test_provider_allowlist():
    with pytest.raises(ProviderNotAllowedError):
        make_provider(lambda _: httpx.Response(200), allowed_providers=["other"])


def test_owned_client_close():
    spec = ProviderSpec(name="test", base_url="https://embedding.test/v1", api_key_env="TEST_KEY")
    with OpenAICompatibleEmbeddingProvider(
        provider=spec, model="m", dimensions=2, revision="v", api_key="unused"
    ) as provider:
        assert not provider._client.is_closed
    assert provider._client.is_closed


def test_cached_api_hits_make_zero_requests_after_restart(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        inputs = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [len(text), 1]}
                    for index, text in enumerate(inputs)
                ]
            },
        )

    provider = make_provider(handler)
    try:
        first = CachedEmbeddingProvider(provider, tmp_path).embed(["source", "issue", "source"])
        assert len(requests) == 1
        requests.clear()
        second = CachedEmbeddingProvider(provider, tmp_path).embed(["source", "issue", "source"])
        np.testing.assert_array_equal(first, second)
        assert requests == []
    finally:
        provider._client.close()


def test_api_batch_size_preserves_global_order():
    requests = []

    def handler(request):
        inputs = json.loads(request.content)["input"]
        requests.append(inputs)
        assert len(inputs) <= 2
        return httpx.Response(
            200,
            json={
                "data": list(
                    reversed(
                        [
                            {"index": index, "embedding": [len(text), 1]}
                            for index, text in enumerate(inputs)
                        ]
                    )
                )
            },
        )

    provider = make_provider(handler, batch_size=2)
    try:
        np.testing.assert_array_equal(
            provider.embed(["a", "bb", "ccc", "d", "ee"]), [[1, 1], [2, 1], [3, 1], [1, 1], [2, 1]]
        )
        assert requests == [["a", "bb"], ["ccc", "d"], ["ee"]]
    finally:
        provider._client.close()


def test_api_invalid_batch_size():
    with pytest.raises(ValueError, match="batch_size"):
        make_provider(lambda _: httpx.Response(200), batch_size=0)
