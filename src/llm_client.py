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

import email.utils
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

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
# Claude Code CLI (subscription auth, no API key)
# --------------------------------------------------------------------------- #
class ClaudeCodeClient(LLMClient):
    """Runs the locally installed Claude Code CLI in headless mode.

    Instead of calling api.anthropic.com with an API key, this shells out to
    ``claude --print``, which reuses the OAuth credentials stored by
    ``claude /login`` — i.e. the Pro/Max subscription of the machine's user.
    No ``ANTHROPIC_API_KEY`` is needed (and if one is set it takes precedence,
    so it is scrubbed from the child process environment).

    Trade-offs vs the HTTP adapter: consumption counts against the
    subscription's rolling usage limits rather than being billed per token,
    ``max_tokens`` cannot be enforced, and each call pays the CLI's startup
    cost (~1-2 s).
    """

    #: Built-in tools disabled by default: this is a pure text-in/text-out use.
    _DEFAULT_DISALLOWED = (
        "Bash Edit Write Read Glob Grep WebFetch WebSearch Task NotebookEdit"
    )

    def __init__(
        self,
        executable: str | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 300.0,
        extra_args: list[str] | None = None,
    ):
        self._exe = executable or shutil.which("claude") or "claude"
        if not shutil.which(self._exe) and not os.path.isfile(self._exe):
            raise LLMError(
                f"Claude Code CLI not found ({self._exe!r}). Install it and run "
                "`claude` once to log in with your subscription account."
            )
        self._max_retries = max_retries
        self._timeout = timeout_seconds
        self._extra_args = list(extra_args or [])
        # Neutral working directory: avoids picking up the CLAUDE.md / settings
        # of whatever project the script happens to be launched from.
        self._cwd = tempfile.mkdtemp(prefix="chrono-cataloger-claude-")

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        cmd = [
            self._exe,
            "--print",
            "--output-format", "json",
            "--model", model,
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", self._DEFAULT_DISALLOWED,
            *self._extra_args,
        ]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        last_err: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=self._timeout,
                    cwd=self._cwd,
                    env=env,
                )
            except subprocess.TimeoutExpired as e:
                last_err = f"timeout after {self._timeout}s"
                if attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise LLMError(f"Claude Code CLI: {last_err}") from e

            if proc.returncode != 0:
                last_err = (proc.stderr or proc.stdout or "").strip()[:500]
                # Usage limits / transient failures are worth retrying.
                if attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise LLMError(
                    f"Claude Code CLI exited with {proc.returncode}: {last_err}"
                )

            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError as e:
                raise LLMError(
                    f"Malformed Claude Code CLI response: {proc.stdout[:500]!r}"
                ) from e

            if data.get("is_error"):
                raise LLMError(f"Claude Code CLI error: {data.get('result')!r}")
            result = data.get("result")
            if not isinstance(result, str):
                raise LLMError(f"Unexpected Claude Code CLI payload: {data!r}")
            return result

        raise LLMError(
            f"Call failed after {self._max_retries + 1} attempts: {last_err}"
        )


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
#: Cap on an honoured Retry-After, so a wild header cannot park the run for hours.
_MAX_RETRY_AFTER_SECONDS = 300.0


def _retry_after_seconds(value: str | None) -> float | None:
    """Seconds to wait per a ``Retry-After`` header, or None if it says nothing.

    RFC 9110 allows both a delay in seconds and an HTTP-date; providers use both,
    and taking ``float()`` of the date form used to raise straight through the
    retry loop — killing the run on exactly the rate limit the retry exists for.
    """
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, min(float(raw), _MAX_RETRY_AFTER_SECONDS))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(delay, _MAX_RETRY_AFTER_SECONDS))


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
                retry_after = _retry_after_seconds(resp.headers.get("retry-after"))
                delay = retry_after if retry_after is not None else min(2 ** attempt, 30)
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
            try:
                return resp.json()
            except ValueError as e:
                # A proxy or a wrong base_url answers 200 with HTML, not JSON.
                raise LLMError(
                    f"Non-JSON response from the provider: {resp.text[:200]!r}"
                ) from e
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
    # An unset timeout means "whatever suits this provider": a subprocess that
    # pays a CLI startup per call, or a local model generating on CPU, needs far
    # more headroom than a hosted HTTP endpoint.
    configured_timeout = llm_cfg.get("timeout_seconds")

    def timeout_or(default: float) -> float:
        return float(configured_timeout) if configured_timeout else default

    if provider == "anthropic":
        api_key = os.environ.get(llm_cfg.get("api_key_env", "ANTHROPIC_API_KEY"), "")
        return AnthropicClient(api_key, base_url, max_retries, timeout_or(120))
    if provider == "claude_code":
        return ClaudeCodeClient(
            executable=llm_cfg.get("claude_cli_path"),
            max_retries=max_retries,
            timeout_seconds=timeout_or(300),
            extra_args=llm_cfg.get("claude_cli_args"),
        )
    if provider == "openai":
        api_key = os.environ.get(llm_cfg.get("api_key_env", "OPENAI_API_KEY"), "")
        return OpenAIClient(api_key, base_url, max_retries, timeout_or(120))
    if provider == "ollama":
        return OllamaClient(base_url, max_retries, timeout_or(300))
    raise LLMError(f"Unsupported LLM provider: {provider!r}")
