"""Interfaccia astratta ai provider LLM.

Il progetto e' volutamente *provider-agnostico*: un'unica interfaccia
:class:`LLMClient` con un adapter per Anthropic (default), OpenAI e Ollama.
Per questo motivo si usano chiamate HTTP diirette via ``httpx`` invece di un
SDK specifico di un singolo provider — cosi' lo stesso codice di triage e
classificazione funziona con qualunque backend senza modifiche.

Formato wire per Anthropic: POST https://api.anthropic.com/v1/messages
con header ``x-api-key`` e ``anthropic-version: 2023-06-01``.
"""
from __future__ import annotations

import os
import time

import httpx


class LLMError(RuntimeError):
    """Errore non recuperabile durante una chiamata all'LLM."""


class LLMClient:
    """Interfaccia comune a tutti i provider."""

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        """Invia un singolo prompt utente e restituisce il testo della risposta."""
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
                "API key Anthropic mancante: imposta la variabile d'ambiente "
                "indicata in llm.api_key_env (default ANTHROPIC_API_KEY)."
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
        # La risposta ha content = lista di blocchi; concateniamo i blocchi 'text'.
        try:
            blocks = data["content"]
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"Risposta Anthropic malformata: {data!r}") from e


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
            raise LLMError("API key OpenAI mancante.")
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
            raise LLMError(f"Risposta OpenAI malformata: {data!r}") from e


# --------------------------------------------------------------------------- #
# Ollama (modello locale)
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
            raise LLMError(f"Risposta Ollama malformata: {data!r}") from e


# --------------------------------------------------------------------------- #
# Helper HTTP con retry/backoff
# --------------------------------------------------------------------------- #
def _post_with_retries(
    url: str,
    headers: dict,
    payload: dict,
    max_retries: int,
    timeout: float,
) -> dict:
    """POST JSON con backoff esponenziale su rate limit (429) ed errori 5xx."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
                last_exc = LLMError(
                    f"HTTP {resp.status_code} dal provider: {resp.text[:200]}"
                )
                if attempt < max_retries:
                    time.sleep(delay)
                    continue
                raise last_exc
            if resp.status_code >= 400:
                raise LLMError(
                    f"HTTP {resp.status_code} dal provider: {resp.text[:500]}"
                )
            return resp.json()
        except httpx.RequestError as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise LLMError(f"Errore di connessione al provider: {e}") from e
    # Non dovrebbe mai arrivare qui
    raise LLMError(f"Chiamata fallita dopo {max_retries + 1} tentativi: {last_exc}")


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def get_client(llm_cfg: dict) -> LLMClient:
    """Costruisce il client giusto in base alla sezione ``llm`` della config."""
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
    raise LLMError(f"Provider LLM non supportato: {provider!r}")
