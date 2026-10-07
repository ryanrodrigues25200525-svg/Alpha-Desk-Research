"""Debate openings run concurrently; later rounds stay sequential (Task 4).

Uses controllable rendezvous barriers (NOT wall-clock timing): the shared
mock LLM blocks each opening invocation on a barrier sized for the debate.
Sequential execution trips the barrier timeout; concurrent openings pass.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import (
    create_conservative_debator,
)
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import (
    GraphSetup,
    run_investment_opening,
    run_risk_opening,
)

_REPORTS = {
    "company_of_interest": "AAPL",
    "asset_type": "stock",
    "market_report": "m",
    "sentiment_report": "s",
    "news_report": "n",
    "fundamentals_report": "f",
}


class RendezvousLLM:
    """Mock LLM that only answers once all debate openings have arrived."""

    def __init__(self, parties: int, timeout: float = 10.0):
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.prompts: list[str] = []
        self.lock = threading.Lock()

    def invoke(self, prompt):
        with self.lock:
            self.prompts.append(prompt)
        # Sequential openings trip this: the first call waits alone and the
        # barrier breaks, failing the test instead of hanging the suite.
        self.barrier.wait()
        if "Bear Analyst making the case" in prompt:
            return SimpleNamespace(content="bear opening")
        if "Bull Analyst" in prompt:
            return SimpleNamespace(content="bull opening")
        if "risk-taking to outpace market norms" in prompt:
            return SimpleNamespace(content="aggressive opening")
        if "conservative stance is ultimately the safest" in prompt:
            return SimpleNamespace(content="conservative opening")
        return SimpleNamespace(content="neutral opening")


def _investment_state():
    return {
        **_REPORTS,
        "investment_debate_state": {
            "history": "",
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "count": 0,
        },
    }


def _risk_state():
    return {
        **_REPORTS,
        "trader_investment_plan": "plan",
        "risk_debate_state": {
            "history": "",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 0,
        },
    }


@pytest.mark.unit
def test_bull_and_bear_open_concurrently():
    llm = RendezvousLLM(parties=2)
    bull_node = create_bull_researcher(llm)
    bear_node = create_bear_researcher(llm)
    merged = run_investment_opening(bull_node, bear_node, _investment_state())
    debate = merged["investment_debate_state"]
    # Both openings saw the empty state: neither rebuts a phantom opponent.
    assert sum("has not spoken yet" in p for p in llm.prompts) == 2
    assert "Bull Analyst: bull opening" in debate["history"]
    assert "Bear Analyst: bear opening" in debate["history"]
    assert debate["bull_history"].strip() == "Bull Analyst: bull opening"
    assert debate["bear_history"].strip() == "Bear Analyst: bear opening"
    assert debate["current_response"] == "Bear Analyst: bear opening"
    assert debate["count"] == 2


@pytest.mark.unit
def test_investment_opening_matches_sequential_result():
    llm = RendezvousLLM(parties=2)
    bull_node = create_bull_researcher(llm)
    bear_node = create_bear_researcher(llm)
    merged = run_investment_opening(bull_node, bear_node, _investment_state())

    seq_llm = RendezvousLLM(parties=1)
    seq_bull = create_bull_researcher(seq_llm)
    seq_bear = create_bear_researcher(seq_llm)
    state = _investment_state()
    state.update(seq_bull(state))
    state.update(seq_bear(state))

    assert merged["investment_debate_state"] == state["investment_debate_state"]


@pytest.mark.unit
def test_risk_openings_run_concurrently():
    llm = RendezvousLLM(parties=3)
    nodes = (
        create_aggressive_debator(llm),
        create_conservative_debator(llm),
        create_neutral_debator(llm),
    )
    merged = run_risk_opening(*nodes, _risk_state())
    debate = merged["risk_debate_state"]
    # Each risk opening saw both opponents as yet to speak.
    assert sum(p.count("has not spoken yet") == 2 for p in llm.prompts) == 3
    assert debate["latest_speaker"] == "Neutral"
    assert debate["count"] == 3
    assert "Aggressive Analyst: aggressive opening" in debate["history"]
    assert "Conservative Analyst: conservative opening" in debate["history"]
    assert "Neutral Analyst: neutral opening" in debate["history"]


@pytest.mark.unit
def test_risk_opening_matches_sequential_result():
    llm = RendezvousLLM(parties=3)
    nodes = (
        create_aggressive_debator(llm),
        create_conservative_debator(llm),
        create_neutral_debator(llm),
    )
    merged = run_risk_opening(*nodes, _risk_state())

    seq_llm = RendezvousLLM(parties=1)
    seq_nodes = (
        create_aggressive_debator(seq_llm),
        create_conservative_debator(seq_llm),
        create_neutral_debator(seq_llm),
    )
    state = _risk_state()
    for node in seq_nodes:
        state.update(node(state))

    assert merged["risk_debate_state"] == state["risk_debate_state"]


@pytest.mark.unit
def test_later_rounds_stay_sequential():
    """After the opening, the wrappers delegate to a single speaker's node."""
    llm = RendezvousLLM(parties=1)
    bull_node = create_bull_researcher(llm)
    bear_node = create_bear_researcher(llm)
    opened = run_investment_opening(bull_node, bear_node, _investment_state())
    state = {**_investment_state(), **opened}

    from tradingagents.graph.setup import investment_opener, risk_opener

    descript = investment_opener(bull_node, bear_node)
    llm2 = RendezvousLLM(parties=1)
    solo_bull = create_bull_researcher(llm2)
    state.update(descript(state))
    expect = {**_investment_state(), **opened}
    expect.update(solo_bull(expect))
    assert state["investment_debate_state"] == expect["investment_debate_state"]

    single_llm = RendezvousLLM(parties=1)
    agg = create_aggressive_debator(single_llm)
    opened_risk = run_risk_opening(
        agg,
        create_conservative_debator(single_llm),
        create_neutral_debator(single_llm),
        _risk_state(),
    )
    rstate = {**_risk_state(), **opened_risk}
    rdebat = rstate["risk_debate_state"]
    # Router still alternates after a parallel opening.
    assert ConditionalLogic().should_continue_risk_analysis(
        {"risk_debate_state": rdebat}
    ) == "Portfolio Manager" if rdebat["count"] >= 3 else "Aggressive Analyst"
    assert risk_opener is not None


@pytest.mark.unit
def test_graph_wires_parallel_openers():
    """setup.py routes the debates through the parallel opening wrappers."""
    from unittest.mock import MagicMock

    setup = GraphSetup(
        quick_thinking_llm=MagicMock(),
        deep_thinking_llm=MagicMock(),
        conditional_logic=ConditionalLogic(),
        max_tool_rounds=1,
    )
    workflow = setup.setup_graph(selected_analysts=("market",))
    for name in ("Bull Researcher", "Aggressive Analyst"):
        runnable = workflow.nodes[name].runnable
        assert getattr(getattr(runnable, "func", runnable), "__parallel_opener__", False)
