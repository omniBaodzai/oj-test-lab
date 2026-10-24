"""Error types for the AI stage. Messages must never contain credentials."""

from __future__ import annotations

import re

_KEY_LIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


class AIError(Exception):
    """Base class. `retryable` tells the provider whether a retry could help."""

    retryable = False


class AIConfigError(AIError):
    pass


class AIAuthError(AIError):
    pass


class AIBillingError(AIError):
    pass


class AIModelError(AIError):
    pass


class AIRequestRejectedError(AIError):
    """HTTP 400/422: the route rejected the request (e.g. unsupported response_format)."""


class AIRateLimitError(AIError):
    retryable = True


class AINetworkError(AIError):
    retryable = True


class AITimeoutError(AIError):
    retryable = True


class AIServiceError(AIError):
    retryable = True


class AIResponseError(AIError):
    """The model answered, but the answer is unusable (malformed JSON, schema violation, truncated)."""


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _KEY_LIKE.sub("***", text)
