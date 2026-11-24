"""Optional LLM reasoning layer, with support for several providers.

THE CRITICAL CONSTRAINT (spec s106): the database is the authoritative state.
The LLM is a reasoning/phrasing layer on top of facts that deterministic
engines have already computed. It is never asked to produce a financial
figure, and its output is never written to a financial column.

The whole platform works with no key at all. When none is configured every
caller falls back to the deterministic path, and the UI says plainly that the
narrative layer is off rather than pretending it ran.

Providers
---------
Any one of these can drive the narrative layer. Several have free tiers, so
the feature does not require a paid account:

    groq        Free tier. Fast. https://console.groq.com/keys
    gemini      Free tier. https://aistudio.google.com/apikey
    openrouter  Free models available. https://openrouter.ai/keys
    ollama      Fully local and offline. https://ollama.com
    anthropic   Paid. https://console.anthropic.com

All are reached over plain HTTPS with ``httpx``, which the project already
depends on, so enabling one installs nothing new.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..config import settings

logger = logging.getLogger(__name__)

#: Rules injected into every prompt. These restate the anti-hallucination
#: requirements of spec s52 in terms the model must follow.
GROUNDING_RULES = """You are the explanation layer of a salary reconciliation system.

ABSOLUTE RULES:
1. Use ONLY the figures in the DATA section. Never invent or estimate a number.
2. Never invent a government order, payment, transaction, deduction, or UTR.
3. If the data does not answer the question, say "I do not have that
   information in your records" and say what document would answer it.
4. Never state that a payment was officially verified unless the data says so.
5. Never allege fraud or wrongdoing. Use neutral language: "unexplained
   difference", "requires verification", "possible duplicate".
6. A bank credit proves money arrived; it does NOT prove official sanction.
   Keep these separate.
7. Quote amounts exactly as given, including the currency symbol.
8. Be brief and plain. The reader may have limited financial literacy.
"""


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    default_model: str
    free_tier: bool
    signup_url: str
    needs_key: bool = True


#: Providers retire model names without notice, so a wrong default shows up as
#: HTTP 404 "model_not_found". :func:`list_models` and the guidance in
#: :func:`_explain_http_error` turn that into an actionable message rather than
#: a dead end.
PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        "groq", "Groq", "openai/gpt-oss-20b", True,
        "https://console.groq.com/keys"),
    "gemini": Provider(
        "gemini", "Google Gemini", "gemini-2.0-flash", True,
        "https://aistudio.google.com/apikey"),
    "openrouter": Provider(
        "openrouter", "OpenRouter", "meta-llama/llama-3.3-70b-instruct:free", True,
        "https://openrouter.ai/keys"),
    "ollama": Provider(
        "ollama", "Ollama (local)", "llama3.2", True,
        "https://ollama.com", needs_key=False),
    "anthropic": Provider(
        "anthropic", "Anthropic Claude", "claude-sonnet-4-5", False,
        "https://console.anthropic.com"),
}


@dataclass
class LLMResult:
    text: str
    used_llm: bool
    error: Optional[str] = None
    provider: Optional[str] = None


class LLMUnavailable(Exception):
    pass


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def provider() -> Optional[Provider]:
    """The provider that will be used, or None if none is configured."""
    name = settings.resolve_llm_provider()
    return PROVIDERS.get(name) if name else None


def model_name() -> str:
    """Configured model, or the provider's default."""
    if settings.llm_model:
        return settings.llm_model
    chosen = provider()
    return chosen.default_model if chosen else ""


def available() -> bool:
    """Whether a usable provider is configured."""
    chosen = provider()
    if chosen is None or not settings.llm_enabled:
        return False
    if not chosen.needs_key:
        return True
    return bool(settings.llm_key_for(chosen.key))


def configuration_problem() -> Optional[str]:
    """Why the layer is off, phrased for an administrator."""
    if not settings.llm_enabled:
        return "LLM_ENABLED is false."

    name = settings.resolve_llm_provider()
    if not name:
        return ("No AI provider is configured. Set one of GROQ_API_KEY, GEMINI_API_KEY, "
                "OPENROUTER_API_KEY or ANTHROPIC_API_KEY in your .env file — Groq and "
                "Gemini both have free tiers.")

    chosen = PROVIDERS.get(name)
    if chosen is None:
        return (f"LLM_PROVIDER is set to '{name}', which is not a supported provider. "
                f"Choose one of: {', '.join(sorted(PROVIDERS))}.")

    if chosen.needs_key and not settings.llm_key_for(chosen.key):
        return (f"LLM_PROVIDER is set to '{name}' but no key is configured. "
                f"Set {chosen.key.upper()}_API_KEY in your .env file "
                f"(get one at {chosen.signup_url}).")
    return None


def status_note(locale: str = "en") -> str:
    """A short line the UI shows so the user knows which mode is active."""
    if available():
        chosen = provider()
        detail = f"{chosen.label} · {model_name()}"
        if locale == "hi":
            return (f"AI विवरण परत सक्रिय है ({detail})। "
                    "सभी राशियाँ आपके रिकॉर्ड से ली गई हैं।")
        return f"AI narrative layer is active ({detail}). All amounts come from your records."

    if locale == "hi":
        return ("AI विवरण परत बंद है। सभी गणनाएँ और उत्तर सिस्टम के "
                "नियम-आधारित इंजन से आ रहे हैं।")
    return ("AI narrative layer is off. All calculations and answers come from the "
            "system's rule-based engines.")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _client():
    import httpx

    return httpx.Client(timeout=float(settings.llm_timeout_seconds))


#: Free tiers cap tokens per minute. A 429 usually clears in a second or two,
#: and the provider says how long to wait, so one short retry converts most of
#: them into a success instead of a silent fallback to deterministic text.
_RETRY_STATUSES = {429, 500, 502, 503, 529}
_MAX_RETRY_WAIT = 8.0


def _retry_after_seconds(response) -> float:
    """How long the provider asked us to wait, capped to something sane."""
    import re

    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), _MAX_RETRY_WAIT)
        except ValueError:
            pass
    # Groq embeds the hint in the message: "Please try again in 3.29s".
    match = re.search(r"try again in ([\d.]+)\s*s", response.text or "", re.I)
    if match:
        try:
            return min(float(match.group(1)) + 0.25, _MAX_RETRY_WAIT)
        except ValueError:
            pass
    return 1.5


def _post(url: str, *, headers: dict, payload: dict) -> dict:
    import time

    attempts = 2
    with _client() as client:
        for attempt in range(attempts):
            response = client.post(url, headers=headers, json=payload)

            if response.status_code in _RETRY_STATUSES and attempt < attempts - 1:
                wait = _retry_after_seconds(response)
                logger.info("Provider returned %s; retrying in %.1fs",
                            response.status_code, wait)
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                raise LLMUnavailable(_explain_http_error(response))
            return response.json()

    raise LLMUnavailable("The provider did not respond.")


def _explain_http_error(response) -> str:
    """Turn a provider error into something an operator can act on."""
    status = response.status_code
    body = (response.text or "")[:300]
    chosen = provider()
    name = chosen.label if chosen else "provider"

    if status in (401, 403):
        return (f"HTTP {status}: {name} rejected the API key. Check the key in your "
                f".env file, or get a new one at {chosen.signup_url if chosen else ''}")

    if status == 404 and "model" in body.lower():
        hint = (f"HTTP 404: the model '{model_name()}' is not available on this "
                f"{name} account. ")
        models = list_models()
        if models:
            hint += ("Models you can use: " + ", ".join(models[:8])
                     + f"\nSet LLM_MODEL in your .env file to one of these.")
        else:
            hint += "Set LLM_MODEL in your .env file to a model your account offers."
        return hint

    if status == 429:
        return (f"HTTP 429: {name} rate limit reached (free tiers cap tokens per "
                f"minute). It usually clears within a minute. Detail: {body}")

    return f"HTTP {status} from provider: {body}"


def list_models() -> list[str]:
    """Chat models the configured account can actually use.

    Returns an empty list when the provider offers no listing endpoint or the
    call fails -- callers treat it as "unknown", never as "none available".
    """
    chosen = provider()
    if chosen is None:
        return []

    api_key = settings.llm_key_for(chosen.key)
    try:
        with _client() as client:
            if chosen.key in ("groq", "openrouter"):
                base = ("https://api.groq.com/openai/v1" if chosen.key == "groq"
                        else "https://openrouter.ai/api/v1")
                response = client.get(f"{base}/models",
                                      headers={"Authorization": f"Bearer {api_key}"})
                ids = [m.get("id", "") for m in response.json().get("data", [])]
            elif chosen.key == "gemini":
                response = client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": api_key})
                ids = [m.get("name", "").replace("models/", "")
                       for m in response.json().get("models", [])]
            elif chosen.key == "ollama":
                response = client.get(f"{settings.ollama_host}/api/tags")
                ids = [m.get("name", "") for m in response.json().get("models", [])]
            else:
                return []
    except Exception as exc:  # noqa: BLE001 - a listing failure must not propagate
        logger.debug("Could not list models for %s: %s", chosen.key, exc)
        return []

    # Drop things that cannot hold a conversation: speech, embedding, vision
    # and safety-classifier models all appear in these listings.
    skip = ("whisper", "tts", "embed", "guard", "orpheus", "vision", "rerank",
            "moderation", "aqa", "imagen", "veo")
    return sorted(m for m in ids if m and not any(s in m.lower() for s in skip))


def _chat_completions(base_url: str, api_key: str, system: str, prompt: str,
                      max_tokens: int, extra_headers: Optional[dict] = None) -> str:
    """OpenAI-compatible endpoint. Used by Groq and OpenRouter."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})

    data = _post(base_url, headers=headers, payload={
        "model": model_name(),
        "max_tokens": max_tokens,
        "temperature": 0,      # deterministic phrasing; this is not creative work
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    })
    choices = data.get("choices") or []
    if not choices:
        raise LLMUnavailable(f"Provider returned no choices: {str(data)[:200]}")

    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content", "") or ""

    # Reasoning models (gpt-oss, o-series, some Qwen builds) spend tokens on a
    # hidden reasoning pass first. If the budget runs out during it, the reply
    # comes back empty with finish_reason "length" -- which looks like a dead
    # provider unless it is named for what it is.
    if not content.strip() and choice.get("finish_reason") == "length":
        used = (data.get("usage") or {}).get("completion_tokens", "?")
        if message.get("reasoning"):
            raise LLMUnavailable(
                f"The model '{model_name()}' is a reasoning model and used its whole "
                f"{used}-token budget on internal reasoning, leaving no answer. "
                f"Raise LLM_MAX_TOKENS in your .env file (try 2000), or choose a "
                f"non-reasoning model.")
        raise LLMUnavailable(
            f"The reply was cut off after {used} tokens before any text was produced. "
            f"Raise LLM_MAX_TOKENS in your .env file.")

    return content


def _gemini(api_key: str, system: str, prompt: str, max_tokens: int) -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model_name()}:generateContent")
    data = _post(url, headers={"Content-Type": "application/json",
                               "x-goog-api-key": api_key},
                 payload={
                     "system_instruction": {"parts": [{"text": system}]},
                     "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                     "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
                 })

    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        raise LLMUnavailable(f"Gemini returned no candidates"
                             + (f" (blocked: {blocked})" if blocked else ""))
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts)


def _ollama(system: str, prompt: str, max_tokens: int) -> str:
    data = _post(f"{settings.ollama_host}/api/chat",
                 headers={"Content-Type": "application/json"},
                 payload={
                     "model": model_name(),
                     "stream": False,
                     "options": {"temperature": 0, "num_predict": max_tokens},
                     "messages": [
                         {"role": "system", "content": system},
                         {"role": "user", "content": prompt},
                     ],
                 })
    return (data.get("message") or {}).get("content", "") or ""


def _anthropic(api_key: str, system: str, prompt: str, max_tokens: int) -> str:
    data = _post("https://api.anthropic.com/v1/messages",
                 headers={"x-api-key": api_key,
                          "anthropic-version": "2023-06-01",
                          "Content-Type": "application/json"},
                 payload={
                     "model": model_name(),
                     "max_tokens": max_tokens,
                     "temperature": 0,
                     "system": system,
                     "messages": [{"role": "user", "content": prompt}],
                 })
    blocks = data.get("content") or []
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def complete(prompt: str, *, system: Optional[str] = None,
             max_tokens: Optional[int] = None) -> LLMResult:
    """Send a grounded prompt. Never raises -- failure degrades gracefully."""
    chosen = provider()
    if chosen is None or not available():
        return LLMResult("", used_llm=False, error=configuration_problem())

    system = system or GROUNDING_RULES
    max_tokens = max_tokens or settings.llm_max_tokens
    api_key = settings.llm_key_for(chosen.key)

    try:
        if chosen.key == "groq":
            text = _chat_completions("https://api.groq.com/openai/v1/chat/completions",
                                     api_key, system, prompt, max_tokens)
        elif chosen.key == "openrouter":
            text = _chat_completions(
                "https://openrouter.ai/api/v1/chat/completions", api_key, system,
                prompt, max_tokens,
                # OpenRouter asks callers to identify themselves.
                extra_headers={"X-Title": "Salary Reconciliation System"})
        elif chosen.key == "gemini":
            text = _gemini(api_key, system, prompt, max_tokens)
        elif chosen.key == "ollama":
            text = _ollama(system, prompt, max_tokens)
        elif chosen.key == "anthropic":
            text = _anthropic(api_key, system, prompt, max_tokens)
        else:
            return LLMResult("", used_llm=False,
                             error=f"Unsupported provider: {chosen.key}")

        text = (text or "").strip()
        if not text:
            return LLMResult("", used_llm=False,
                             error="The provider returned an empty response.",
                             provider=chosen.key)
        return LLMResult(text, used_llm=True, provider=chosen.key)

    except Exception as exc:  # noqa: BLE001 - any failure must degrade, not crash
        logger.warning("LLM call to %s failed, using deterministic output: %s",
                       chosen.key, exc)
        return LLMResult("", used_llm=False, error=str(exc), provider=chosen.key)


def complete_json(prompt: str, *, system: Optional[str] = None,
                  max_tokens: Optional[int] = None) -> tuple[Optional[dict], bool]:
    """Ask for a JSON object. Returns ``(parsed_or_None, used_llm)``.

    A malformed response yields ``None`` rather than a half-parsed dict, so a
    caller can never act on a partially understood answer.
    """
    result = complete(prompt, system=system, max_tokens=max_tokens)
    if not result.used_llm or not result.text:
        return None, False

    text = result.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, True
    try:
        return json.loads(text[start:end + 1]), True
    except json.JSONDecodeError:
        logger.warning("LLM returned malformed JSON; ignoring it.")
        return None, True


def build_data_block(payload: dict[str, Any]) -> str:
    """Serialise grounded facts for a prompt's DATA section."""
    return "DATA (the only facts you may use):\n" + json.dumps(payload, indent=2,
                                                               ensure_ascii=False, default=str)


def test_connection() -> tuple[bool, str]:
    """Round-trip check used by ``manage.py testllm``.

    Returns ``(ok, message)``. Calls the provider for real, so it proves the
    key works rather than merely that one is present.
    """
    problem = configuration_problem()
    if problem:
        return False, problem

    chosen = provider()
    result = complete(
        "Reply with exactly the word: READY",
        system="You are a connection test. Reply with one word and nothing else.",
        # Generous, because a reasoning model spends tokens before answering.
        max_tokens=256,
    )
    if result.used_llm:
        return True, (f"{chosen.label} responded using model '{model_name()}'. "
                      f"Reply: {result.text[:60]!r}")
    return False, f"{chosen.label} did not respond: {result.error}"
