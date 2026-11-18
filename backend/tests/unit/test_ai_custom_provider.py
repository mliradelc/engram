"""Tests for the user-supplied custom AI provider.

The custom provider lets a user point Engram at any OpenAI-compatible endpoint
with their own API key, instead of being limited to the built-in slugs. These
tests pin the behaviour that makes it usable: the URL is assembled correctly
from whatever form the user pasted, the key is sent as a bearer token, a missing
address or model is reported as a configuration error rather than silently
falling back, and failures name the endpoint.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_httpx(response_json: dict, status: int = 200):
    """Build a mocked httpx.AsyncClient context manager with one POST response."""
    response = MagicMock()
    response.json.return_value = response_json
    response.status_code = status
    response.raise_for_status = MagicMock()
    if status >= 400:
        from httpx import HTTPStatusError, Request, Response

        req = Request("POST", "http://x")
        response.raise_for_status.side_effect = HTTPStatusError(
            "err", request=req, response=Response(status, request=req)
        )

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)
    return client


def _ok_body():
    return {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]}


class TestCustomChatCompletionsUrl:
    """The user pastes a base URL; the adapter must not double up the path."""

    def test_v1_suffix_gets_the_path_appended(self):
        from app.core.ai_client import custom_chat_completions_url

        assert (
            custom_chat_completions_url("https://api.example.com/v1")
            == "https://api.example.com/v1/chat/completions"
        )

    def test_bare_host_gets_the_path_appended(self):
        from app.core.ai_client import custom_chat_completions_url

        assert (
            custom_chat_completions_url("https://api.example.com")
            == "https://api.example.com/chat/completions"
        )

    def test_a_full_path_is_left_alone(self):
        """Appending blindly here would produce /chat/completions/chat/completions."""
        from app.core.ai_client import custom_chat_completions_url

        assert (
            custom_chat_completions_url("https://api.example.com/v1/chat/completions")
            == "https://api.example.com/v1/chat/completions"
        )

    def test_trailing_slash_is_normalised(self):
        from app.core.ai_client import custom_chat_completions_url

        assert (
            custom_chat_completions_url("https://api.example.com/v1/")
            == "https://api.example.com/v1/chat/completions"
        )

    def test_blank_returns_blank(self):
        from app.core.ai_client import custom_chat_completions_url

        assert custom_chat_completions_url("") == ""
        assert custom_chat_completions_url("   ") == ""


class TestResolveAiBaseUrl:
    """One helper picks the right address so no call site branches on provider."""

    def test_custom_provider_uses_the_custom_field(self):
        from app.core.ai_client import resolve_ai_base_url

        assert (
            resolve_ai_base_url("custom", "http://localhost:11434/v1", "https://gw.example.com/v1")
            == "https://gw.example.com/v1"
        )

    def test_local_provider_uses_the_local_field(self):
        from app.core.ai_client import resolve_ai_base_url

        assert (
            resolve_ai_base_url("ollama", "http://box:11434/v1", "https://gw.example.com/v1")
            == "http://box:11434/v1"
        )

    def test_remote_builtin_needs_no_address(self):
        from app.core.ai_client import resolve_ai_base_url

        assert (
            resolve_ai_base_url("openai", "http://box:11434/v1", "https://gw.example.com/v1") == ""
        )


class TestCustomProviderIsKnown:
    def test_custom_is_a_known_provider(self):
        from app.core.ai_client import KNOWN_PROVIDERS

        assert "custom" in KNOWN_PROVIDERS

    def test_custom_is_not_treated_as_local(self):
        """A custom endpoint is usually remote and authenticated; the local path
        would give it a 5-minute timeout and skip the key requirement."""
        from app.core.ai_client import LOCAL_PROVIDERS

        assert "custom" not in LOCAL_PROVIDERS

    def test_custom_has_no_default_model(self):
        from app.core.ai_client import DEFAULT_MODELS

        assert "custom" not in DEFAULT_MODELS

    def test_custom_counts_as_configured_without_a_key(self):
        """Self-hosted gateways often need no key; requiring one would disable
        them at every one of the four config gates."""
        from app.core.ai_client import ai_is_configured

        assert ai_is_configured("custom", "") is True


class TestCompleteJsonCustom:
    @pytest.mark.asyncio
    async def test_posts_to_the_custom_endpoint_with_a_bearer_token(self):
        from app.core.ai_client import complete_json

        mock = _mock_httpx(_ok_body())
        with patch("app.core.ai_client.httpx.AsyncClient", return_value=mock):
            result = await complete_json(
                prompt="identify this disc",
                schema=None,
                provider="custom",
                api_key="sk-custom-123",
                model="my-model-v1",
                base_url="https://gw.example.com/v1",
            )

        assert result == {"ok": True}
        call = mock.post.await_args
        assert call.args[0] == "https://gw.example.com/v1/chat/completions"
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk-custom-123"
        assert call.kwargs["json"]["model"] == "my-model-v1"

    @pytest.mark.asyncio
    async def test_a_keyless_endpoint_still_sends_an_authorization_header(self):
        """Some gateways reject a blank header; others ignore it. Sending a
        placeholder is the only form that works for both."""
        from app.core.ai_client import complete_json

        mock = _mock_httpx(_ok_body())
        with patch("app.core.ai_client.httpx.AsyncClient", return_value=mock):
            await complete_json(
                prompt="x",
                schema=None,
                provider="custom",
                api_key="",
                model="m",
                base_url="https://gw.example.com/v1",
            )

        assert mock.post.await_args.kwargs["headers"]["Authorization"] == "Bearer local"

    @pytest.mark.asyncio
    async def test_a_blank_base_url_is_a_configuration_error(self):
        from app.core.ai_client import complete_json
        from app.core.errors import AIProviderError

        with pytest.raises(AIProviderError) as exc:
            await complete_json(
                prompt="x",
                schema=None,
                provider="custom",
                api_key="sk-x",
                model="m",
                base_url="",
                raise_on_error=True,
            )

        assert exc.value.code == "bad_request"
        assert "endpoint address" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_blank_model_is_a_configuration_error(self):
        from app.core.ai_client import complete_json
        from app.core.errors import AIProviderError

        with pytest.raises(AIProviderError) as exc:
            await complete_json(
                prompt="x",
                schema=None,
                provider="custom",
                api_key="sk-x",
                model="",
                base_url="https://gw.example.com/v1",
                raise_on_error=True,
            )

        assert exc.value.code == "bad_request"

    @pytest.mark.asyncio
    async def test_an_unsafe_base_url_is_refused_before_any_request(self):
        from app.core.ai_client import complete_json
        from app.core.errors import AIProviderError

        mock = _mock_httpx(_ok_body())
        with patch("app.core.ai_client.httpx.AsyncClient", return_value=mock):
            with pytest.raises(AIProviderError) as exc:
                await complete_json(
                    prompt="x",
                    schema=None,
                    provider="custom",
                    api_key="sk-x",
                    model="m",
                    base_url="http://user:pass@gw.example.com/v1",
                    raise_on_error=True,
                )

        assert exc.value.code == "bad_request"
        mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_without_raise_on_error_a_bad_config_returns_none(self):
        """The bulk matcher must not start raising because a user misconfigured
        the endpoint; it falls back like any other unusable provider."""
        from app.core.ai_client import complete_json

        result = await complete_json(
            prompt="x",
            schema=None,
            provider="custom",
            api_key="sk-x",
            model="m",
            base_url="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_a_rejected_key_names_the_endpoint(self):
        from app.core.ai_client import complete_json
        from app.core.errors import AIProviderError

        mock = _mock_httpx({"error": "unauthorized"}, status=401)
        with patch("app.core.ai_client.httpx.AsyncClient", return_value=mock):
            with pytest.raises(AIProviderError) as exc:
                await complete_json(
                    prompt="x",
                    schema=None,
                    provider="custom",
                    api_key="sk-bad",
                    model="m",
                    base_url="https://gw.example.com/v1",
                    raise_on_error=True,
                )

        assert exc.value.code == "bad_key"
        assert "https://gw.example.com/v1" in str(exc.value)

    @pytest.mark.asyncio
    async def test_an_unknown_model_is_classified_as_model_unavailable(self):
        from app.core.ai_client import complete_json
        from app.core.errors import AIProviderError

        mock = _mock_httpx({"error": "no such model"}, status=404)
        with patch("app.core.ai_client.httpx.AsyncClient", return_value=mock):
            with pytest.raises(AIProviderError) as exc:
                await complete_json(
                    prompt="x",
                    schema=None,
                    provider="custom",
                    api_key="sk-x",
                    model="nope",
                    base_url="https://gw.example.com/v1",
                    raise_on_error=True,
                )

        assert exc.value.code == "model_unavailable"
        assert "nope" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_json_schema_request_still_works(self):
        """The identification path passes a schema; the custom adapter must send
        a response_format rather than dropping it."""
        from app.core.ai_client import complete_json

        mock = _mock_httpx(_ok_body())
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        with patch("app.core.ai_client.httpx.AsyncClient", return_value=mock):
            await complete_json(
                prompt="respond as JSON",
                schema=schema,
                provider="custom",
                api_key="sk-x",
                model="m",
                base_url="https://gw.example.com/v1",
            )

        body = mock.post.await_args.kwargs["json"]
        assert body["response_format"] == {"type": "json_object"}
