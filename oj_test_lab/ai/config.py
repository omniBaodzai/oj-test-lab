"""Configuration from OPENROUTER_* settings only.

Sources (later wins): `.env` file (only the OPENROUTER_* keys are picked, the rest of
the file is ignored) < process environment (same keys only). os.environ is never
modified and no other variable is read.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import AIConfigError

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 4000
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES_LIMIT = 5
MAX_TOKENS_LIMIT = 32_000

KEYS = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_MODEL",
    "OPENROUTER_TIMEOUT_SECONDS",
    "OPENROUTER_MAX_OUTPUT_TOKENS",
    "OPENROUTER_MAX_RETRIES",
)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class AIConfig:
    api_key: str = field(repr=False)  # never shown in repr/str
    model: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT
    max_output_tokens: int = DEFAULT_MAX_TOKENS
    max_retries: int = DEFAULT_MAX_RETRIES


def _int(name: str, raw: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise AIConfigError(f"{name} must be an integer") from None
    if not lo <= value <= hi:
        raise AIConfigError(f"{name} must be between {lo} and {hi}")
    return value


def _check_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return url.rstrip("/")
    raise AIConfigError("OPENROUTER_BASE_URL must be an https:// URL (http is only allowed for localhost)")


def load_config(
    env_file: Path | None = Path(".env"),
    env: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    require_api_key: bool = True,
) -> AIConfig:
    values: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        from dotenv import dotenv_values

        values.update({k: v for k, v in dotenv_values(env_file).items() if k in KEYS and v})
    source = os.environ if env is None else env
    values.update({k: source[k] for k in KEYS if source.get(k)})

    api_key = values.get("OPENROUTER_API_KEY", "").strip()
    if require_api_key and not api_key:
        raise AIConfigError(
            "OPENROUTER_API_KEY is not set. Put it in your environment or in .env "
            "(see .env.example); it is never read from command line arguments."
        )
    if require_api_key and (len(api_key) > 512 or any(c.isspace() for c in api_key)):
        raise AIConfigError("OPENROUTER_API_KEY has an invalid format")

    chosen_model = (model or values.get("OPENROUTER_MODEL", "")).strip()
    if not chosen_model:
        raise AIConfigError("no model selected: set OPENROUTER_MODEL or pass --model (calls are paid, so there is no default)")
    if not _MODEL_RE.match(chosen_model):
        raise AIConfigError("model id has an invalid format")

    base_url = _check_base_url(values.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL).strip())
    timeout = DEFAULT_TIMEOUT
    if "OPENROUTER_TIMEOUT_SECONDS" in values:
        timeout = float(_int("OPENROUTER_TIMEOUT_SECONDS", values["OPENROUTER_TIMEOUT_SECONDS"], 1, 900))
    tokens = max_output_tokens
    if tokens is None:
        tokens = _int("OPENROUTER_MAX_OUTPUT_TOKENS", values.get("OPENROUTER_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_TOKENS)), 256, MAX_TOKENS_LIMIT)
    elif not 256 <= tokens <= MAX_TOKENS_LIMIT:
        raise AIConfigError(f"max output tokens must be between 256 and {MAX_TOKENS_LIMIT}")
    retries = _int("OPENROUTER_MAX_RETRIES", values.get("OPENROUTER_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)), 0, MAX_RETRIES_LIMIT)
    return AIConfig(api_key, chosen_model, base_url, timeout, tokens, retries)
