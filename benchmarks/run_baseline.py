"""Baseline benchmark: one full-graph run, wall-clock per phase + token totals.

Compares against Tasks 2-5 optimizations. Requires LLM + vendor API keys in
env (see .env.example); without them the run fails and this script still
writes a JSON envelope recording the failure so the gap is explicit.

Phase boundaries come from ``TradingAgentsGraph.stream_run`` state keys:
analyst reports land when the slowest analyst files (fan-in), then
investment_plan (debate -> Research Manager), trader_investment_plan,
final_trade_decision (risk -> Portfolio Manager / manager).

Usage:
    python benchmarks/run_baseline.py --ticker AAPL
    python benchmarks/run_baseline.py --ticker AAPL --out benchmarks/results/task2.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler

from tradingagents import __version__ as ta_version
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# First phase milestone (in stream order) each state key marks. Keys are
# checked in this order per streamed state; a key already seen is skipped so
# repeated states don't double-count.
PHASE_KEYS = (
    ("analysts", ("market_report", "sentiment_report", "news_report", "fundamentals_report")),
    ("debate", ("investment_plan",)),
    ("trader", ("trader_investment_plan",)),
    ("risk_manager", ("final_trade_decision",)),
)

# Analyst report keys; the analysts phase starts when the first one lands and
# ends when all four have landed (they fan-in before the debate).
ANALYST_REPORT_KEYS = ("market_report", "sentiment_report", "news_report", "fundamentals_report")


# Fixed baseline date (a Friday with full vendor bars) so later tasks compare
# deltas against the same date; overridable via --trade-date.
BASELINE_TRADE_DATE = "2024-06-14"


def default_trade_date() -> str:
    """Fixed trade date for comparable baselines."""
    return BASELINE_TRADE_DATE


def summarize_usage(handler: UsageMetadataCallbackHandler) -> dict:
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    by_model: dict[str, dict] = {}
    for model, usage in handler.usage_metadata.items():
        entry = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        by_model[model] = entry
        for k in total:
            total[k] += entry[k]
    return {"total": total, "by_model": by_model}


def run(ticker: str, trade_date: str) -> dict:
    usage = UsageMetadataCallbackHandler()
    trade_date = trade_date or default_trade_date()
    config = DEFAULT_CONFIG.copy()

    ta = TradingAgentsGraph(debug=False, config=config, callbacks=[usage])
    init_state = ta.create_run_state(ticker, trade_date)
    args = ta.propagator.get_graph_args(callbacks=[usage])

    phases: dict[str, dict] = {}
    analyst_first_seen: float | None = None
    analyst_done_at: float | None = None
    started = time.perf_counter()
    marks: dict[str, float] = {}
    final_state: dict = {}
    for _messages, state in ta.stream_run(init_state, **args):
        now = time.perf_counter()
        if state is None:
            continue
        final_state = state
        if analyst_first_seen is None and any(state.get(k) for k in ANALYST_REPORT_KEYS):
            analyst_first_seen = now
        if analyst_done_at is None and all(state.get(k) for k in ANALYST_REPORT_KEYS):
            analyst_done_at = now
            marks["analysts"] = now
        for phase, keys in PHASE_KEYS[1:]:
            if phase not in marks and any(state.get(k) for k in keys):
                marks[phase] = now

    ended = time.perf_counter()
    prev = started
    ordered = ["analysts", "debate", "trader", "risk_manager"]
    for phase in ordered:
        at = marks.get(phase)
        phases[phase] = {
            "wall_s": round(at - prev, 2) if at is not None else None,
            "cumulative_s": round(at - started, 2) if at is not None else None,
            "completed": at is not None,
        }
        if at is not None:
            prev = at
    phases["manager"] = {
        # Portfolio Manager writes final_trade_decision via the risk fan-in;
        # anything after that mark is report/logging overhead.
        "wall_s": round(ended - prev, 2),
        "cumulative_s": round(ended - started, 2),
        "completed": True,
    }
    phases["analysts"]["first_report_s"] = (
        round(analyst_first_seen - started, 2) if analyst_first_seen is not None else None
    )

    tokens = summarize_usage(usage)
    result = {
        "ticker": ticker,
        "trade_date": trade_date,
        "tradingagents_version": ta_version,
        "run_settings": ta.run_settings(),
        "wall_total_s": round(ended - started, 2),
        "phases": phases,
        "tokens": tokens,
        "decision": str(final_state.get("final_trade_decision", ""))[:500],
        "ok": True,
    }
    ta.record_decision(ticker, trade_date, final_state)
    ta.save_reports(final_state, ticker)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Baseline benchmark: full-graph run + timings.")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--trade-date", default=None)
    parser.add_argument("--out", default="benchmarks/results/baseline.json")
    ns = parser.parse_args(argv)
    trade_date = ns.trade_date or default_trade_date()

    out = Path(ns.out)
    try:
        result = run(ns.ticker, trade_date)
    except Exception as exc:  # keys missing, vendor/LLM down: record, don't block
        result = {
            "ticker": ns.ticker,
            "trade_date": trade_date,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=10),
            "hint": "Baseline needs LLM + vendor API keys in env (see .env.example).",
        }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2)[:2000])
    print(f"wrote {out}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
