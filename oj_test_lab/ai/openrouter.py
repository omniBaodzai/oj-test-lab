"""OpenRouter (OpenAI-compatible) provider with timeout, bounded retries and error mapping."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import openai

from .config import AIConfig
from .errors import (
    AIAuthError,
    AIBillingError,
    AIError,
    AIModelError,
    AINetworkError,
    AIRateLimitError,
    AIRequestRejectedError,
    AIResponseError,
    AIServiceError,
    AITimeoutError,
    redact,
)
from .provider import ProviderReply

MAX_RESPONSE_CHARS = 200_000
BACKOFF_BASE = 1.0
BACKOFF_CAP = 20.0


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        config: AIConfig,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = config.model
        self._config = config
        self._sleep = sleep
        # max_retries=0: all retrying happens in _with_retries so the policy is explicit and testable.
        self._client = client or openai.OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    def _detail(self, exc: Exception) -> str:
        text = str(getattr(exc, "message", "") or "").strip().replace("\n", " ")[:200]
        return redact(text, (self._config.api_key,))

    def _classify(self, exc: Exception) -> AIError:
        detail = self._detail(exc)
        suffix = f": {detail}" if detail else ""
        if isinstance(exc, openai.APITimeoutError):
            return AITimeoutError(f"request timed out after {self._config.timeout_seconds:g}s")
        if isinstance(exc, openai.APIConnectionError):
            return AINetworkError("cannot reach the AI service (network error)")
        if isinstance(exc, openai.APIStatusError):
            code = exc.status_code
            if code in (401, 403):
                return AIAuthError(f"authentication failed (HTTP {code}); check OPENROUTER_API_KEY and key permissions{suffix}")
            if code == 402:
                return AIBillingError(f"insufficient credits or billing problem (HTTP 402); top up your OpenRouter account{suffix}")
            if code == 404:
                return AIModelError(f"model not found or no provider route available for {self.model!r} (HTTP 404){suffix}")
            if code == 408:
                return AITimeoutError("the AI service reported a request timeout (HTTP 408)")
            if code == 429:
                return AIRateLimitError(f"rate limited (HTTP 429){suffix}")
            if code >= 500:
                return AIServiceError(f"AI service error (HTTP {code}){suffix}")
            if code in (400, 422):
                return AIRequestRejectedError(f"request rejected (HTTP {code}){suffix}")
            return AIServiceError(f"unexpected HTTP status {code}{suffix}")
        return AIError(f"unexpected {type(exc).__name__} while calling the AI service")

    def _with_retries(self, call: Callable[[], Any]) -> Any:
        attempts = self._config.max_retries + 1
        for attempt in range(attempts):
            try:
                return call()
            except AIError:
                raise
            except Exception as exc:  # SDK/transport exceptions only; mapped below
                error = self._classify(exc)
                if not error.retryable:
                    raise error from None
                if attempt == attempts - 1:
                    raise type(error)(f"{error} (gave up after {attempts} attempts)") from None
                self._sleep(min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt))
        raise AssertionError("unreachable")

    def _create(self, messages: list[dict[str, str]], response_format: dict[str, Any], max_tokens: int) -> Any:
        return self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=0,
        )

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int,
    ) -> ProviderReply:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        structured = {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
        try:
            response = self._with_retries(lambda: self._create(messages, structured, max_output_tokens))
        except AIRequestRejectedError:
            # The route may not support json_schema: fall back once to plain JSON mode.
            # Output is validated against the same schema either way.
            try:
                response = self._with_retries(
                    lambda: self._create(messages, {"type": "json_object"}, max_output_tokens)
                )
            except AIRequestRejectedError as exc:
                raise AIModelError(f"the model/route rejected the request, even in JSON mode: {exc}") from None
        return self._read(response)

    def _read(self, response: Any) -> ProviderReply:
        choices = getattr(response, "choices", None)
        if not choices:
            raise AIResponseError("the AI service returned no choices")
        choice = choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise AIResponseError("the response was truncated (max output tokens reached); raise --max-tokens")
        content = getattr(getattr(choice, "message", None), "content", None)
        if not isinstance(content, str) or not content.strip():
            raise AIResponseError("the AI service returned an empty message")
        if len(content) > MAX_RESPONSE_CHARS:
            raise AIResponseError("the AI response is unreasonably large")
        reported = getattr(response, "model", "")
        return ProviderReply(content, reported if isinstance(reported, str) else "")
