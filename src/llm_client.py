"""Abstract interface to the LLM providers.

The project is deliberately *provider-agnostic*: a single :class:`LLMClient`
interface with one adapter for Anthropic (default), OpenAI and Ollama. That is
why direct HTTP calls through ``httpx`` are used instead of a provider-specific
SDK — so the same triage and classification code works with any backend without
changes.

Wire format for Anthropic: POST https://api.anthropic.com/v1/messages
with the ``x-api-key`` header and ``anthropic-version: 2023-06-01``.
"""
from __future__ import annotations

import os
import time

import httpx


class LLMError(RuntimeError):
    """Unrecoverable error during an LLM call."""


class LLMClient:
    """Interface common to every provider."""

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        """Send a single user prompt and return the response text."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #
class AnthropicClient(LLMClient):
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 120.0,
    ):
        if not api_key:
            raise LLMError(
                "Missing Anthropic API key: set the environment variable "
                "named in llm.api_key_env (ANTHROPIC_API_KEY by default)."
            )
        self._api_key = api_key
        self._base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self._max_retries = max_retries
        self._timeout = timeout_seconds

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        url = f"{self._base_url}/v1/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = _post_with_retries(
            url, headers, payload, self._max_retries, self._timeout
        )
        # The response has content = a list of blocks; concatenate the 'text' ones.
        try:
            blocks = data["content"]
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"Malformed Anthropic response: {data!r}") from e


# --------------------------------------------------------------------------- #
# OpenAI (Chat Completions)
# --------------------------------------------------------------------------- #
class OpenAIClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 120.0,
    ):
        if not api_key:
            raise LLMError("Missing OpenAI API key.")
        self._api_key = api_key
        self._base_url = (base_url or "https://api.openai.com").rstrip("/")
        self._max_retries = max_retries
        self._timeout = timeout_seconds

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        url = f"{self._base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = _post_with_retries(
            url, headers, payload, self._max_retries, self._timeout
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Malformed OpenAI response: {data!r}") from e


# --------------------------------------------------------------------------- #
# Ollama (local model)
# --------------------------------------------------------------------------- #
class OllamaClient(LLMClient):
    def __init__(
        self,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 300.0,
    ):
        self._base_url = (base_url or "http://localhost:11434").rstrip("/")
        self._max_retries = max_retries
        self._timeout = timeout_seconds

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        url = f"{self._base_url}/api/generate"
        headers = {"content-type": "application/json"}
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        data = _post_with_retries(
            url, headers, payload, self._max_retries, self._timeout
        )
        try:
            return data["response"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"Malformed Ollama response: {data!r}") from e


# --------------------------------------------------------------------------- #
# HTTP helper with retry/backoff
# --------------------------------------------------------------------------- #
def _post_with_retries(
    url: str,
    headers: dict,
    payload: dict,
    max_retries: int,
    timeout: float,
) -> dict:
    """POST JSON with exponential backoff on rate limits (429) and 5xx errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
                last_exc = LLMError(
                    f"HTTP {resp.status_code} from the provider: {resp.text[:200]}"
                )
                if attempt < max_retries:
                    time.sleep(delay)
                    continue
                raise last_exc
            if resp.status_code >= 400:
                raise LLMError(
                    f"HTTP {resp.status_code} from the provider: {resp.text[:500]}"
                )
            return resp.json()
        except httpx.RequestError as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise LLMError(f"Connection error to the provider: {e}") from e
    # Should never get here
    raise LLMError(f"Call failed after {max_retries + 1} attempts: {last_exc}")


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def get_client(llm_cfg: dict) -> LLMClient:
    """Build the right client based on the ``llm`` section of the config."""
    provider = llm_cfg.get("provider", "anthropic")
    base_url = llm_cfg.get("base_url")
    max_retries = int(llm_cfg.get("max_retries", 3))
    timeout = float(llm_cfg.get("timeout_seconds", 120))

    if provider == "anthropic":
        api_key = os.environ.get(llm_cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
        return AnthropicClient(api_key, base_url, max_retries, timeout)
    if provider == "openai":
        api_key = os.environ.get(llm_cfg.get("api_key_env", "OPENAI_API_KEY"), "")
        return OpenAIClient(api_key, base_url, max_retries, timeout)
    if provider == "ollama":
        return OllamaClient(base_url, max_retries, timeout)
    raise LLMError(f"Unsupported LLM provider: {provider!r}")
