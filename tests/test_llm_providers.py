"""The optional AI narrative layer (spec s52, s106).

Two things must hold no matter which provider is configured:

  * the platform works completely without any provider, and
  * a provider can never introduce a financial figure of its own.

These tests mock the HTTP transport, so they verify request construction and
the safety guards without needing a real API key or making network calls.
"""

from __future__ import annotations

import pytest

from app.agents import llm
from app.config import settings

PROVIDER_RESPONSES = {
    "groq": {"choices": [{"message": {"content": "Hello from Groq"}}]},
    "openrouter": {"choices": [{"message": {"content": "Hello from OpenRouter"}}]},
    "gemini": {"candidates": [{"content": {"parts": [{"text": "Hello from Gemini"}]}}]},
    "ollama": {"message": {"content": "Hello from Ollama"}},
    "anthropic": {"content": [{"type": "text", "text": "Hello from Claude"}]},
}

KEY_FIELDS = ("groq_api_key", "gemini_api_key", "openrouter_api_key", "anthropic_api_key")


@pytest.fixture
def llm_env(monkeypatch):
    """Configure a provider without touching the real environment."""
    def configure(provider="auto", model="", enabled=True, **keys):
        for field in KEY_FIELDS:
            monkeypatch.setattr(settings, field, "")
        monkeypatch.setattr(settings, "llm_provider", provider)
        monkeypatch.setattr(settings, "llm_model", model)
        monkeypatch.setattr(settings, "llm_enabled", enabled)
        for field, value in keys.items():
            monkeypatch.setattr(settings, field, value)
    return configure


@pytest.fixture
def transport(monkeypatch):
    """Intercept the HTTP call and record what was sent."""
    captured: dict = {}

    def fake_post(url, *, headers, payload):
        captured.update(url=url, headers=headers, payload=payload)
        return PROVIDER_RESPONSES[settings.resolve_llm_provider()]

    monkeypatch.setattr(llm, "_post", fake_post)
    return captured


# ---------------------------------------------------------------------------
# Working without any provider
# ---------------------------------------------------------------------------
def test_platform_works_with_no_provider(llm_env):
    llm_env()
    assert llm.available() is False
    assert llm.provider() is None

    result = llm.complete("anything")
    assert result.used_llm is False
    assert result.text == ""
    assert "No AI provider is configured" in result.error


def test_status_note_is_honest_when_off(llm_env):
    llm_env()
    for locale in ("en", "hi"):
        note = llm.status_note(locale)
        assert "rule-based" in note or "नियम-आधारित" in note


def test_disabling_the_layer_overrides_a_present_key(llm_env):
    llm_env(groq_api_key="gsk_something", enabled=False)
    assert llm.available() is False
    assert "LLM_ENABLED is false" in llm.configuration_problem()


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field,expected,model", [
    ("groq_api_key", "groq", "openai/gpt-oss-20b"),
    ("gemini_api_key", "gemini", "gemini-2.0-flash"),
    ("openrouter_api_key", "openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ("anthropic_api_key", "anthropic", "claude-sonnet-4-5"),
])
def test_provider_detected_from_its_key(llm_env, field, expected, model):
    llm_env(**{field: "test-key-value"})
    assert llm.provider().key == expected
    assert llm.model_name() == model
    assert llm.available() is True


def test_auto_prefers_a_free_provider(llm_env):
    """With both configured, the free tier should be chosen over the paid one."""
    llm_env(groq_api_key="gsk_free", anthropic_api_key="sk-ant-paid")
    assert llm.provider().key == "groq"
    assert llm.provider().free_tier is True


def test_explicit_provider_overrides_auto_detection(llm_env):
    llm_env(provider="gemini", groq_api_key="gsk_present")
    assert llm.provider().key == "gemini"
    # Named without a key: reported as misconfigured, not silently swapped.
    assert llm.available() is False
    assert "GEMINI_API_KEY" in llm.configuration_problem()


def test_ollama_needs_no_key(llm_env):
    llm_env(provider="ollama")
    assert llm.available() is True
    assert llm.configuration_problem() is None


def test_custom_model_overrides_the_default(llm_env):
    llm_env(provider="groq", groq_api_key="gsk_x", model="llama-3.1-8b-instant")
    assert llm.model_name() == "llama-3.1-8b-instant"


def test_unknown_provider_is_reported_clearly(llm_env):
    llm_env(provider="grokk")
    assert llm.available() is False
    problem = llm.configuration_problem()
    assert "not a supported provider" in problem
    assert "groq" in problem      # the valid options are listed


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------
def test_groq_uses_the_openai_compatible_endpoint(llm_env, transport):
    llm_env(provider="groq", groq_api_key="gsk_secret")
    result = llm.complete("Say hello")

    assert result.used_llm is True
    assert result.text == "Hello from Groq"
    assert transport["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert transport["headers"]["Authorization"] == "Bearer gsk_secret"
    assert transport["payload"]["model"] == "openai/gpt-oss-20b"
    assert transport["payload"]["temperature"] == 0


def test_gemini_puts_the_model_in_the_url_and_key_in_a_header(llm_env, transport):
    llm_env(provider="gemini", gemini_api_key="AIza_secret")
    result = llm.complete("Say hello")

    assert result.text == "Hello from Gemini"
    assert "gemini-2.0-flash:generateContent" in transport["url"]
    assert transport["headers"]["x-goog-api-key"] == "AIza_secret"
    # The key must never end up in the query string, where it would be logged.
    assert "AIza_secret" not in transport["url"]


def test_openrouter_identifies_the_caller(llm_env, transport):
    llm_env(provider="openrouter", openrouter_api_key="sk-or-secret")
    result = llm.complete("Say hello")

    assert result.text == "Hello from OpenRouter"
    assert transport["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert "X-Title" in transport["headers"]


def test_ollama_talks_to_the_configured_host(llm_env, transport, monkeypatch):
    monkeypatch.setattr(settings, "ollama_host", "http://192.168.1.50:11434")
    llm_env(provider="ollama")
    result = llm.complete("Say hello")

    assert result.text == "Hello from Ollama"
    assert transport["url"] == "http://192.168.1.50:11434/api/chat"
    assert transport["payload"]["stream"] is False


def test_anthropic_sends_the_version_header(llm_env, transport):
    llm_env(provider="anthropic", anthropic_api_key="sk-ant-secret")
    result = llm.complete("Say hello")

    assert result.text == "Hello from Claude"
    assert transport["headers"]["x-api-key"] == "sk-ant-secret"
    assert transport["headers"]["anthropic-version"] == "2023-06-01"


def test_grounding_rules_are_always_sent(llm_env, transport):
    llm_env(provider="groq", groq_api_key="gsk_x")
    llm.complete("Say hello")

    system = transport["payload"]["messages"][0]["content"]
    assert "Never invent or estimate a number" in system
    assert "Never allege fraud" in system


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------
def test_provider_failure_degrades_instead_of_raising(llm_env, monkeypatch):
    def boom(url, *, headers, payload):
        raise RuntimeError("HTTP 401: invalid_api_key")

    monkeypatch.setattr(llm, "_post", boom)
    llm_env(provider="groq", groq_api_key="wrong-key")

    result = llm.complete("anything")      # must not raise
    assert result.used_llm is False
    assert "invalid_api_key" in result.error


def test_empty_response_is_treated_as_failure(llm_env, monkeypatch):
    monkeypatch.setattr(llm, "_post",
                        lambda url, *, headers, payload: {"choices": [{"message": {"content": ""}}]})
    llm_env(provider="groq", groq_api_key="gsk_x")

    result = llm.complete("anything")
    assert result.used_llm is False


def test_malformed_json_response_yields_none(llm_env, monkeypatch):
    monkeypatch.setattr(
        llm, "_post",
        lambda url, *, headers, payload: {
            "choices": [{"message": {"content": "{not valid json at all"}}]})
    llm_env(provider="groq", groq_api_key="gsk_x")

    parsed, used = llm.complete_json("give me json")
    assert parsed is None      # never a half-understood dict


# ---------------------------------------------------------------------------
# The safety guard that matters most
# ---------------------------------------------------------------------------
def test_invented_figures_are_discarded(llm_env, monkeypatch):
    """A provider must never be able to introduce a number of its own."""
    from app.agents.query_agent import Answer, _polish

    monkeypatch.setattr(llm, "_post", lambda url, *, headers, payload: {
        "choices": [{"message": {"content":
                                 "You received ₹99,999.00 and ₹12.34 is pending."}}]})
    llm_env(provider="groq", groq_api_key="gsk_x")

    answer = Answer(text="You received ₹50,500.00 in total during 2026.",
                    intent="total_received")
    assert _polish("How much did I receive?", answer, "en") is None, (
        "a response containing figures absent from the computed answer "
        "must be thrown away entirely"
    )


def test_faithful_rewording_is_accepted(llm_env, monkeypatch):
    from app.agents.query_agent import Answer, _polish

    monkeypatch.setattr(llm, "_post", lambda url, *, headers, payload: {
        "choices": [{"message": {"content":
                                 "In 2026 you were paid ₹50,500.00 altogether."}}]})
    llm_env(provider="groq", groq_api_key="gsk_x")

    answer = Answer(text="You received ₹50,500.00 in total during 2026.",
                    intent="total_received")
    polished = _polish("How much did I receive?", answer, "en")
    assert polished is not None
    assert "50,500.00" in polished


def test_assistant_answer_is_unchanged_when_the_provider_fails(
        db, org, employee, periods, aww_rule, llm_env, monkeypatch):
    """A dead provider must not change a single figure the user sees."""
    from datetime import date

    from app.agents import query_agent
    from conftest import add_credit, run_pipeline

    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="LLM1")
    run_pipeline(db, employee)

    llm_env()      # no provider
    without = query_agent.ask(db, employee, "How much money is pending?", use_llm=False)

    def boom(url, *, headers, payload):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(llm, "_post", boom)
    llm_env(provider="groq", groq_api_key="gsk_x")
    with_broken = query_agent.ask(db, employee, "How much money is pending?", use_llm=True)

    assert with_broken.text == without.text
    assert with_broken.used_llm is False
