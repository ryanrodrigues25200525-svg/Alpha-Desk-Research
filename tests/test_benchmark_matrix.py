"""Per-config cost matrix: config overrides and the matrix aggregation.

Only the pure pieces are tested here — the merge onto DEFAULT_CONFIG and the
per-config aggregation. Nothing here runs the graph, an LLM or the network;
the streaming runs themselves need API keys and live in the smoke path.
"""

from __future__ import annotations

import pytest

from alpha_desk_research.default_config import DEFAULT_CONFIG
from benchmarks.run_baseline import merged_config
from benchmarks.run_matrix import summarize_matrix


@pytest.mark.unit
def test_overrides_are_merged_over_the_defaults():
    cfg = merged_config({"deep_think_llm": "cheap", "max_debate_rounds": 1})
    assert (
        cfg["deep_think_llm"] == "cheap"
        and cfg["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    )


@pytest.mark.unit
def test_the_matrix_summarizes_cost_per_config():
    table = summarize_matrix(
        [
            {
                "config": "a",
                "tokens": {"total": {"total_tokens": 100}},
                "wall_total_s": 9.0,
                "ok": True,
            },
            {
                "config": "a",
                "tokens": {"total": {"total_tokens": 300}},
                "wall_total_s": 11.0,
                "ok": False,
            },
        ]
    )
    assert (
        table[0]["config"] == "a"
        and table[0]["runs"] == 2
        and table[0]["ok"] == 1
        and table[0]["median_tokens"] == 200
    )
