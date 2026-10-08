import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.analysts.turn import WRAP_UP
from tradingagents.agents.state import AgentState

from .analyst_execution import build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

logger = logging.getLogger(__name__)

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. under prompt/i18n/
# refactor drift in the speaker labels) can never hit a missing path_map entry
# and crash LangGraph mid-run (#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}


def _tools_or_done(state) -> str:
    """Route an analyst's turn: run its tool calls, or finish with its report."""
    return "tools" if state["messages"][-1].tool_calls else END


def _analyst_graph(spec, agent, max_tool_rounds: int):
    """One analyst as a graph of its own: the model and its tools, on a private message history.

    It returns only its report, so analysts running side by side never write the
    same key, and its tool calls never reach the other analysts' messages. After
    ``max_tool_rounds`` rounds of tool calls it is told to write its report, and
    that turn ends it whatever it answers, so a model that keeps calling tools
    cannot run the graph into its recursion limit (#1420).
    """
    output = TypedDict(f"{spec.key.capitalize()}Report", {spec.report_key: str})
    graph = StateGraph(AgentState, output_schema=output)
    graph.add_node("agent", agent)
    graph.add_edge(START, "agent")
    if not spec.tools:
        graph.add_edge("agent", END)
        return graph.compile()

    def calls(messages):
        return [call["name"] for m in messages for call in (getattr(m, "tool_calls", None) or [])]

    def rounds(messages) -> int:
        return sum(1 for m in messages if getattr(m, "tool_calls", None))

    def more_or_wrap_up(state) -> str:
        return "wrap_up" if rounds(state["messages"]) >= max_tool_rounds else "agent"

    def wrap_up(state):
        repeated = ", ".join(f"{name} x{n}" for name, n in Counter(calls(state["messages"])).most_common())
        logger.warning("%s used its %d tool rounds (%s); asking for its report",
                       spec.agent_node, max_tool_rounds, repeated)
        return agent({**state, "messages": [*state["messages"], HumanMessage(WRAP_UP)]})

    graph.add_node("tools", ToolNode(list(spec.tools)))
    graph.add_node("wrap_up", wrap_up)
    graph.add_conditional_edges("agent", _tools_or_done, ["tools", END])
    graph.add_conditional_edges("tools", more_or_wrap_up, ["agent", "wrap_up"])
    graph.add_edge("wrap_up", END)
    return graph.compile()


def run_investment_opening(bull_node: Callable, bear_node: Callable, state: dict) -> dict:
    """Run both research openings concurrently and merge them deterministically.

    Both nodes read the same pre-debate state, so neither opening rebuts a
    phantom opponent. The merged update equals sequential bull-then-bear, with
    the canonical Bull-before-Bear history order and count 2.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        bull_update, bear_update = tuple(
            pool.map(lambda node: node(state), (bull_node, bear_node))
        )
    bull_debate = bull_update["investment_debate_state"]
    bear_debate = bear_update["investment_debate_state"]
    base = state["investment_debate_state"]
    bull_argument = bull_debate["history"][len(base.get("history", "")) + 1:]
    bear_argument = bear_debate["history"][len(base.get("history", "")) + 1:]
    return {
        "investment_debate_state": {
            "history": base.get("history", "") + "\n" + bull_argument + "\n" + bear_argument,
            "bull_history": base.get("bull_history", "") + "\n" + bull_argument,
            "bear_history": base.get("bear_history", "") + "\n" + bear_argument,
            "current_response": bear_argument,
            "count": base.get("count", 0) + 2,
        }
    }


def run_risk_opening(aggressive_node: Callable, conservative_node: Callable, neutral_node: Callable, state: dict) -> dict:
    """Run all three risk openings concurrently; merge equals sequential order."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        aggressive_update, conservative_update, neutral_update = tuple(
            pool.map(
                lambda node: node(state),
                (aggressive_node, conservative_node, neutral_node),
            )
        )
    base = state["risk_debate_state"]
    prefix = len(base.get("history", "")) + 1
    aggressive_argument = aggressive_update["risk_debate_state"]["history"][prefix:]
    conservative_argument = conservative_update["risk_debate_state"]["history"][prefix:]
    neutral_argument = neutral_update["risk_debate_state"]["history"][prefix:]
    return {
        "risk_debate_state": {
            "history": (
                base.get("history", "")
                + "\n" + aggressive_argument
                + "\n" + conservative_argument
                + "\n" + neutral_argument
            ),
            "aggressive_history": base.get("aggressive_history", "") + "\n" + aggressive_argument,
            "conservative_history": base.get("conservative_history", "") + "\n" + conservative_argument,
            "neutral_history": base.get("neutral_history", "") + "\n" + neutral_argument,
            "latest_speaker": "Neutral",
            "current_aggressive_response": aggressive_argument,
            "current_conservative_response": conservative_argument,
            "current_neutral_response": neutral_argument,
            "count": base.get("count", 0) + 3,
        }
    }


def investment_opener(bull_node: Callable, bear_node: Callable) -> Callable:
    """Graph node: parallel opening on count 0, else delegate to one speaker.

    The debate router alternates by current_response, so later rounds run one
    node at a time exactly as before.
    """
    def opener(state: dict) -> dict:
        if state["investment_debate_state"]["count"] == 0:
            return run_investment_opening(bull_node, bear_node, state)
        if state["investment_debate_state"]["current_response"].startswith("Bull"):
            return bear_node(state)
        return bull_node(state)

    opener.__parallel_opener__ = True
    return opener


def risk_opener(aggressive_node: Callable, conservative_node: Callable, neutral_node: Callable) -> Callable:
    """Graph node: parallel opening on count 0, else delegate to one speaker."""
    def opener(state: dict) -> dict:
        if state["risk_debate_state"]["count"] == 0:
            return run_risk_opening(
                aggressive_node, conservative_node, neutral_node, state
            )
        speaker = state["risk_debate_state"]["latest_speaker"]
        if speaker.startswith("Aggressive"):
            return conservative_node(state)
        if speaker.startswith("Conservative"):
            return neutral_node(state)
        return aggressive_node(state)

    opener.__parallel_opener__ = True
    return opener



class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        conditional_logic: ConditionalLogic,
        max_tool_rounds: int,
    ):
        """Initialize with required components."""
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.conditional_logic = conditional_logic
        self.max_tool_rounds = max_tool_rounds

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals"), memory_node=None
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Sentiment analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
            memory_node: Node that settles past decisions and returns the run's
                ``past_context``. It runs alongside the analysts.
        """
        plan = build_analyst_execution_plan(selected_analysts)

        analyst_factories = {
            "market": lambda: create_market_analyst(self.quick_thinking_llm),
            "social": lambda: create_sentiment_analyst(self.quick_thinking_llm),
            "news": lambda: create_news_analyst(self.quick_thinking_llm),
            "fundamentals": lambda: create_fundamentals_analyst(self.quick_thinking_llm),
        }

        bull_researcher_node = create_bull_researcher(self.quick_thinking_llm)
        bear_researcher_node = create_bear_researcher(self.quick_thinking_llm)
        research_manager_node = create_research_manager(self.deep_thinking_llm)
        trader_node = create_trader(self.quick_thinking_llm)

        aggressive_analyst = create_aggressive_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        conservative_analyst = create_conservative_debator(self.quick_thinking_llm)
        portfolio_manager_node = create_portfolio_manager(self.deep_thinking_llm)

        workflow = StateGraph(AgentState)

        for spec in plan.specs:
            workflow.add_node(spec.agent_node,
                              _analyst_graph(spec, analyst_factories[spec.key](), self.max_tool_rounds))

        workflow.add_node(
            "Bull Researcher", investment_opener(bull_researcher_node, bear_researcher_node)
        )
        workflow.add_node(
            "Bear Researcher", investment_opener(bull_researcher_node, bear_researcher_node)
        )
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node(
            "Aggressive Analyst",
            risk_opener(aggressive_analyst, conservative_analyst, neutral_analyst),
        )
        workflow.add_node(
            "Neutral Analyst",
            risk_opener(aggressive_analyst, conservative_analyst, neutral_analyst),
        )
        workflow.add_node(
            "Conservative Analyst",
            risk_opener(aggressive_analyst, conservative_analyst, neutral_analyst),
        )
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # The analysts work at the same time; the research debate starts once
        # every one of them has filed its report. The memory log settles past
        # decisions alongside them: only the Portfolio Manager reads its lessons.
        first_steps = [spec.agent_node for spec in plan.specs]
        if memory_node is not None:
            workflow.add_node("Memory Log", memory_node)
            first_steps.append("Memory Log")
        for node in first_steps:
            workflow.add_edge(START, node)
        workflow.add_edge(first_steps, "Bull Researcher")

        # Both research-debate edges share the complete DEBATE_PATH_MAP (#1088).
        for debate_node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(
                debate_node,
                self.conditional_logic.should_continue_debate,
                DEBATE_PATH_MAP,
            )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        # All three risk edges share the complete RISK_ANALYSIS_PATH_MAP (#1088).
        for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(
                risk_node,
                self.conditional_logic.should_continue_risk_analysis,
                RISK_ANALYSIS_PATH_MAP,
            )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
