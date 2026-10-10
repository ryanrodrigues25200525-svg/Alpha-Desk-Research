"""Tests for ALPHA_DESK_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import os

import pytest

import alpha_desk_research.default_config as default_config_module


def _config_with_env(monkeypatch, **overrides):
    """The defaults as the environment given here would set them."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return default_config_module.build_default_config()


def test_no_env_uses_built_in_defaults(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["llm_provider"] == "openai"
    assert config["deep_think_llm"] == "gpt-6-sol"
    assert config["quick_think_llm"] == "gpt-6-luna"
    assert config["backend_url"] is None
    assert config["max_debate_rounds"] == 1
    assert config["checkpoint_enabled"] is False


def test_string_overrides(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        ALPHA_DESK_LLM_PROVIDER="google",
        ALPHA_DESK_DEEP_THINK_LLM="gemini-3-pro-preview",
        ALPHA_DESK_QUICK_THINK_LLM="gemini-3-flash-preview",
        ALPHA_DESK_LLM_BACKEND_URL="https://example.invalid/v1",
        ALPHA_DESK_OUTPUT_LANGUAGE="Chinese",
    )
    assert config["llm_provider"] == "google"
    assert config["deep_think_llm"] == "gemini-3-pro-preview"
    assert config["quick_think_llm"] == "gemini-3-flash-preview"
    assert config["backend_url"] == "https://example.invalid/v1"
    assert config["output_language"] == "Chinese"


def test_int_coercion(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        ALPHA_DESK_MAX_DEBATE_ROUNDS="3",
        ALPHA_DESK_MAX_RISK_ROUNDS="2",
    )
    assert config["max_debate_rounds"] == 3
    assert isinstance(config["max_debate_rounds"], int)
    assert config["max_risk_discuss_rounds"] == 2
    assert isinstance(config["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(monkeypatch, raw, expected):
    config = _config_with_env(monkeypatch, ALPHA_DESK_CHECKPOINT_ENABLED=raw)
    assert config["checkpoint_enabled"] is expected


def test_reasoning_thinking_overrides(monkeypatch):
    """The provider reasoning/thinking knobs are env-configurable (non-interactive runs)."""
    config = _config_with_env(
        monkeypatch,
        ALPHA_DESK_OPENAI_REASONING_EFFORT="high",
        ALPHA_DESK_GOOGLE_THINKING_LEVEL="minimal",
        ALPHA_DESK_ANTHROPIC_EFFORT="low",
    )
    assert config["openai_reasoning_effort"] == "high"
    assert config["google_thinking_level"] == "minimal"
    assert config["anthropic_effort"] == "low"


def test_reasoning_effort_defaults_to_none(monkeypatch):
    """Unset reasoning/thinking knobs stay None so each provider uses its own default."""
    config = _config_with_env(monkeypatch)
    assert config["openai_reasoning_effort"] is None
    assert config["google_thinking_level"] is None
    assert config["anthropic_effort"] is None


def test_empty_env_value_is_passthrough(monkeypatch):
    """Empty ALPHA_DESK_* values must not clobber the built-in default."""
    config = _config_with_env(
        monkeypatch,
        ALPHA_DESK_LLM_PROVIDER="",
        ALPHA_DESK_MAX_DEBATE_ROUNDS="",
    )
    assert config["llm_provider"] == "openai"
    assert config["max_debate_rounds"] == 1


def test_empty_path_value_keeps_the_default_path(monkeypatch):
    """.env.example lists the path variables blank; uncommenting one made the
    path empty, and the graph failed creating its directories."""
    config = _config_with_env(
        monkeypatch,
        ALPHA_DESK_RESULTS_DIR="",
        ALPHA_DESK_CACHE_DIR="",
        ALPHA_DESK_MEMORY_LOG_PATH="",
    )
    home = default_config_module._ALPHA_DESK_HOME
    assert config["results_dir"] == os.path.join(home, "logs")
    assert config["data_cache_dir"] == os.path.join(home, "cache")
    assert config["memory_log_path"] == os.path.join(home, "memory", "trading_memory.md")


def test_invalid_int_raises(monkeypatch):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    monkeypatch.setenv("ALPHA_DESK_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError, match="ALPHA_DESK_MAX_DEBATE_ROUNDS"):
        default_config_module.build_default_config()


@pytest.mark.parametrize("bad", ["treu", "flase", "maybe", "2", "enabled"])
def test_invalid_bool_raises(monkeypatch, bad):
    """A misspelled boolean must fail loudly (like ints) instead of silently False."""
    monkeypatch.setenv("ALPHA_DESK_CHECKPOINT_ENABLED", bad)
    with pytest.raises(ValueError, match="ALPHA_DESK_CHECKPOINT_ENABLED"):
        default_config_module.build_default_config()


def test_unknown_env_var_is_ignored(monkeypatch):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    config = _config_with_env(
        monkeypatch,
        ALPHA_DESK_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in config
