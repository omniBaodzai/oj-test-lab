"""Provider abstraction. Add Gemini etc. by implementing `AIProvider`; analyzer and CLI don't change."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderReply:
    text: str  # raw model output: UNTRUSTED
    model_reported: str = ""


class AIProvider(Protocol):
    name: str  # e.g. "openrouter"
    model: str  # requested model id

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int,
    ) -> ProviderReply:
        """Return the model's JSON text. Must raise `AIError` subclasses (with credential-free
        messages) on failure, apply its own timeout/retry policy, and never execute the output."""
        ...
