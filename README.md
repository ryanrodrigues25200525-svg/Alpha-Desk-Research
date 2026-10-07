# Alpha Desk Research

> A multi-agent LLM equity research desk — analysts, debate, trader, risk, portfolio manager — callable from the CLI **or any MCP client** (Claude, IDEs, agents).

Forked from [TauricResearch/TradingAgents](https://github.com/tauricresearch/tradingagents) (v0.6.0, Apache-2.0 — see [ATTRIBUTION](#attribution)), then optimized for cost/latency and wrapped with a native MCP job server.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](mcp_server/)

---

## What it does

Give it a ticker + date. A team of LLM agents researches it like a real desk:

```
┌──────────────┐     ┌──────────────┐     ┌──────────┐     ┌──────┐     ┌───────────┐
│  ANALYSTS    │────▶│    DEBATE    │────▶│  TRADER  │────▶│ RISK │────▶│ PORTFOLIO │
│ market       │     │ bull vs bear │     │  plan    │     │ 3    │     │ MANAGER   │
│ sentiment    │     │ research mgr │     │          │     │ stances   │ decision  │
│ news         │     │ decides      │     │          │     │      │     │           │
│ fundamentals │     │              │     │          │     │      │     │           │
└──────────────┘     └──────────────┘     └──────────┘     └──────┘     └───────────┘
       4 parallel reports → investment plan → execution → risk check → BUY/SELL/HOLD
```

Output: a full report tree — per-analyst notes, bull/bear cases, trader plan, risk debates, final decision — as Markdown + one HTML page.

**Live-tested:** full AAPL run in **94s, 117k tokens, $0** (free-tier model). See [`benchmarks/results/live_openrouter.json`](benchmarks/results/live_openrouter.json).

## What's new vs upstream

| Area | Change |
|---|---|
| **MCP server** | `mcp_server/` — stdio job server: `run_analysis(ticker, date)` → `get_report` / `get_status`. Long debate graphs run as background jobs with progress, not blocking tool calls |
| **Vendor output caps** | Truncated tool payloads + TTL cache in `dataflows/` — fewer wasted input tokens per run |
| **Prompt trims** | Tighter analyst prompts, trimmed tool catalog, model-tiering docs |
| **Graph parallelism** | Debate/risk openings fan out in parallel instead of serial |
| **CLI hardening** | Thread-safe live display, `--no-live` flag for headless/CI, non-zero exit on failure |
| **Config** | `TRADINGAGENTS_*` env overrides incl. per-tier provider + free-model friendly |

## Quickstart

```bash
pip install -e ".[mcp]"
cp .env.example .env   # add at least one LLM key
```

**CLI:**

```bash
alpha-desk --ticker AAPL --date 2024-06-14        # interactive live view
alpha-desk --ticker AAPL --date 2024-06-14 --no-live  # headless / CI
```

Cheapest live run (free models via OpenRouter):

```bash
export OPENROUTER_API_KEY=...
export TRADINGAGENTS_LLM_PROVIDER=openrouter
export TRADINGAGENTS_QUICK_THINK_LLM="nvidia/nemotron-3-super-120b-a12b:free"
export TRADINGAGENTS_DEEP_THINK_LLM="nvidia/nemotron-3-super-120b-a12b:free"
alpha-desk --ticker AAPL --no-live
```

**MCP server (Claude / agents):**

```json
{
  "mcpServers": {
    "alpha-desk": { "command": "alpha-desk-mcp", "cwd": "/path/to/alpha-desk-research" }
  }
}
```

Tools: `run_analysis` → `get_status` → `get_report`. Contract tests: `tests/test_mcp_contract.py`.

**Benchmark a run:**

```bash
python benchmarks/run_baseline.py --ticker AAPL --out benchmarks/results/my_run.json
```

## Repo layout

```
tradingagents/     agent graph, analysts, dataflows (vendors), LLM clients
cli/               terminal UI (live view + --no-live)
mcp_server/        stdio job server over the graph (jobs / server / worker)
benchmarks/        run harness + recorded results (timing, tokens, decision)
tests/             1285 passing incl. MCP contract, CLI flags, parallel debate
```

## Attribution

Built on [TradingAgents](https://github.com/tauricresearch/tradingagents) by Tauric Research ([paper](https://arxiv.org/abs/2412.20138), Apache-2.0). Upstream history is not included in this repo (fresh root); see [CHANGELOG.md](CHANGELOG.md) for upstream release notes through v0.6.0. All changes above that base are ours, released under the same license.
