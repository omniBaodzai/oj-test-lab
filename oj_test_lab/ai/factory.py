"""Factory kept separate so the CLI (and tests) can swap providers without touching the analyzer."""

from __future__ import annotations

from .config import AIConfig
from .errors import AIConfigError
from .provider import AIProvider


def build_provider(config: AIConfig, name: str = "openrouter") -> AIProvider:
    if name == "openrouter":
        from .openrouter import OpenRouterProvider  # lazy: `openai` is only needed for real calls

        return OpenRouterProvider(config)
    raise AIConfigError(f"unknown AI provider {name!r}")
