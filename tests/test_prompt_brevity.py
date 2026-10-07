"""Analyst brevity caps (spec s1d-1e).

Each analyst report must stay under 600 words, and the market analyst's
indicator catalog must be names + one-liners (not multi-sentence prose).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

FACTORIES = ["market", "fundamentals", "news", "sentiment"]


def _base_state():
    return {
        "company_of_interest": "AAPL",
        "trade_date": "2026-08-14",
        "asset_type": "stock",
        "instrument_context": "",
        "portfolio_context": "",
        "past_context": "",
        "messages": [],
    }


def _capture_tool_analyst(mod, factory, state):
    """Render a tool-calling analyst's system prompt without calling any model."""
    captured = {}

    def fake_take_turn(prompt, llm, tools, messages):
        msgs = prompt.format_messages(messages=[])
        captured["text"] = "\n".join(str(getattr(m, "content", "")) for m in msgs)
        return AIMessage(content="r"), "r"

    with patch.object(mod, "take_turn", fake_take_turn):
        factory(MagicMock())(state)
    return captured["text"]


def _capture_sentiment(state):
    import tradingagents.agents.analysts.sentiment_analyst as mod

    captured = {}

    def fake_invoke(structured_llm, llm, messages, render, name):
        captured["text"] = "\n".join(str(getattr(m, "content", "")) for m in messages)
        return "r"

    with (
        patch.object(mod, "jev_screen", return_value=None),
        patch.object(mod, "fetch_stocktwits_messages", return_value="bullish sample"),
        patch.object(mod, "fetch_reddit_posts", return_value="reddit sample"),
        patch.object(mod, "invoke_structured_or_freetext", side_effect=fake_invoke),
        patch.object(mod.get_news, "func", return_value="headline sample"),
    ):
        mod.create_sentiment_analyst(MagicMock())(state)
    return captured["text"]


def get_system_prompt(factory: str) -> str:
    """Render the analyst's full system prompt text (test helper)."""
    import tradingagents.agents.analysts.fundamentals_analyst as fundamentals
    import tradingagents.agents.analysts.market_analyst as market
    import tradingagents.agents.analysts.news_analyst as news

    state = _base_state()
    if factory == "market":
        return _capture_tool_analyst(market, market.create_market_analyst, state)
    if factory == "fundamentals":
        return _capture_tool_analyst(
            fundamentals, fundamentals.create_fundamentals_analyst, state
        )
    if factory == "news":
        return _capture_tool_analyst(news, news.create_news_analyst, state)
    if factory == "sentiment":
        return _capture_sentiment(state)
    raise ValueError(f"unknown analyst factory: {factory}")


@pytest.mark.unit
@pytest.mark.parametrize("factory", FACTORIES)
def test_analyst_prompt_has_brevity_cap(factory):
    assert "600 words" in get_system_prompt(factory)


@pytest.mark.unit
def test_market_indicator_catalog_trimmed():
    prompt = get_system_prompt("market")
    catalog = prompt[prompt.index("Moving Averages") : prompt.index("Select indicators")]
    assert len(catalog) < 1500
