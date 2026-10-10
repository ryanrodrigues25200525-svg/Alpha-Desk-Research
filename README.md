# 📊 Alpha Desk Research

> 🤖 A team of AI analysts that researches stocks for you — just give it a ticker, get back a full report.

🧠 Analysts → 🐂🐻 Debate → 📋 Trader plan → 🛡️ Risk check → ✅ Final decision (BUY / SELL / HOLD)

Forked from [TauricResearch/TradingAgents](https://github.com/tauricresearch/tradingagents) (v0.6.0, Apache-2.0 — see [Attribution](#-attribution)), then made faster, cheaper, and callable from Claude / IDEs / agents via MCP. 💸⚡

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](mcp_server/)

---

## ✨ What does it do?

You say: **"Research AAPL"** 📣

It does this 👇

```
📝 ANALYSTS  ──▶  🐂🐻 DEBATE  ──▶  📋 TRADER  ──▶  🛡️ RISK  ──▶  ✅ DECISION
   market            bull vs bear       plan          3 views       BUY/SELL/HOLD
   sentiment         manager picks      to buy/sell   safe vs bold  with reasons
   news              winner                         risky vs calm
   fundamentals
```

📦 You get: a folder of reports — one per analyst, the debate notes, the trader's plan, and the final call — as Markdown + one pretty HTML page. 🌐

🚀 **Proven live:** full AAPL report in **94 seconds, $0** (free model). See [`benchmarks/results/live_openrouter.json`](benchmarks/results/live_openrouter.json). 🎉

## 🆕 What did we add?

| Area | Plain English |
|---|---|
| 🤖 **MCP server** | Claude / agents can call it as tools: `run_analysis` → `get_status` → `get_report`. Long runs happen in the background with progress. 📡 |
| ✂️ **Smaller data dumps** | Vendor data gets trimmed + cached, so you pay fewer tokens per run. 💰 |
| ✍️ **Shorter prompts** | Analysts get tighter instructions — same brains, less rambling. 🧹 |
| ⚡ **Parallel debates** | Bull/bear/risk agents talk at the same time instead of waiting in line. 🏃‍♂️🏃‍♀️ |
| 🖥️ **Tougher CLI** | Live view that doesn't glitch, `--no-live` mode for servers/CI, fails loudly on errors. 🔔 |
| ⚙️ **Easy config** | Set models/keys via `ALPHA_DESK_*` env vars, including free-model presets. 🔑 |
| 🎯 **Track record** | The desk keeps score of its own calls: `alpha-desk scoreboard` prints hit rate + mean alpha per rating group, and says "too few to rate" until the sample is real. 📈 |

## 🚀 Quickstart

📥 Install:

```bash
pip install -e ".[mcp]"
cp .env.example .env   # ✏️ add at least one LLM key
```

⌨️ **Run in your terminal:**

```bash
alpha-desk --ticker AAPL --date 2024-06-14            # 👀 live view
alpha-desk --ticker AAPL --date 2024-06-14 --no-live   # 🤖 headless / CI
```

💸 **Cheapest run (free models via OpenRouter):**

```bash
export OPENROUTER_API_KEY=...
export ALPHA_DESK_LLM_PROVIDER=openrouter
export ALPHA_DESK_QUICK_THINK_LLM="nvidia/nemotron-3-super-120b-a12b:free"
export ALPHA_DESK_DEEP_THINK_LLM="nvidia/nemotron-3-super-120b-a12b:free"
alpha-desk --ticker AAPL --no-live
```

🤖 **Use from Claude / agents (MCP):**

```json
{
  "mcpServers": {
    "alpha-desk": { "command": "alpha-desk-mcp", "cwd": "/path/to/alpha-desk-research" }
  }
}
```

🧰 Tools: `submit_research_job` 🏁 → `job_status` 👀 → `get_report` 📄, plus `list_reports`, `cancel_research_job`, `get_decision` and `get_scoreboard` 🎯. Tests: `tests/test_mcp_contract.py`. ✅

⏱️ **Time your own run:**

```bash
python benchmarks/run_baseline.py --ticker AAPL --out benchmarks/results/my_run.json
```

📈 **Score the desk's own calls:**

```bash
alpha-desk scoreboard                      # hit rate + mean alpha, by rating
alpha-desk scoreboard --group-by model     # split it by the model that made the call
alpha-desk scoreboard --log path/to/memory.md   # score a backtest run's log instead
```

📊 **Cost a config, then judge it:** `python benchmarks/run_matrix.py --configs configs.json` writes a cost/latency table per config; quality for each comes from `alpha-desk scoreboard --log <that run's log>`. 📉

## 🗂️ What's inside?

```
alpha_desk_research/  🧠 the agent team + data + models
cli/                  🖥️ the terminal app
mcp_server/           📡 the Claude/agent plug (jobs / server / worker)
benchmarks/           ⏱️ speed + cost measurements
tests/                ✅ 1353 passing tests (3 known macOS-only failures)
```

## 🙏 Attribution

Built on [TradingAgents](https://github.com/tauricresearch/tradingagents) by Tauric Research ([paper](https://arxiv.org/abs/2412.20138), Apache-2.0). Upstream history isn't in this repo (fresh start); see [CHANGELOG.md](CHANGELOG.md) for upstream notes through v0.6.0. Everything new here is ours, same license. ❤️
