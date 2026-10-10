# Settle and Usage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the scoreboard's loop — settle due decisions from a scheduler with no analysis run, and record what each run cost (provider-reported), so the board can say what a row's calls earned *and* cost.

**Architecture:** The usage counter moves out of the CLI into `alpha_desk_research/usage.py` (the graph is about to need it too), deduped by `run_id` because the CLI attaches one handler at both the LLM clients and the run config — measured, one call today counts twice. The OpenRouter provider row asks for its own cost accounting (`usage.include`), `RunRecord` gains `tokens_in`/`tokens_out`/`cost_usd`, and the graph writes them from the caller's counter or one of its own. Settling becomes one module-level helper (`settle_configured`) with a CLI command and an MCP tool over the same JSON, and `scoreboard` gains a usage line per row.

**Tech Stack:** Python 3.11+, dataclasses, `typer` (CLI), `fastmcp` (MCP), `pytest` (tests), `rich` (CLI output). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-11-settle-and-usage-design.md`

## Global Constraints

- **Cost is the provider's own number or nothing.** OpenRouter reports it when the request sets `extra_body={"usage": {"include": True}}`; no price table, no estimation. `cost_usd is None` means "not reported"; `0.0` is a reported zero (free models). Never conflate the two.
- **All-or-nothing cost per run.** A run's `cost_usd` is set only when every LLM call it made reported one (`cost_calls == llm_calls` and `llm_calls > 0`); a partial sum would read as the whole bill. Otherwise `null`.
- **One handler attached twice counts once.** Dedupe `on_llm_start`/`on_chat_model_start`/`on_llm_end`/`on_tool_start` by `run_id` (verified: the same `run_id` arrives on both attachments).
- **`RunRecord` changes are additive.** A P1-era line (no usage keys) must load with zeros and `None`; an unknown key is still dropped; `append`/`load` keep their tolerance.
- **Nothing settles silently wrong.** A missing model or a failed reflection leaves the entry pending and is reported; instrumentation never fails the run it describes.
- **The board's existing text is unchanged where the data has nothing to say.** For an index with no usage fields, `render()` output must be byte-identical to today's — pinned by a test.
- All tests run offline (`tests/conftest.py` refuses network) and must pass with `python3 -m pytest`. `ruff check .` stays clean.
- Python 3.11+; no new dependencies.

## Review Focus

Inputs and conditions the spec implies but that no obvious test covers — each gets a test in the task that owns the code:

1. **A langchain version that stops delivering the same `run_id` to both attachments** would silently double-count again. *(Task 1: pin the double-attach case with a fake model, and assert the two deliveries carry the same `run_id`.)*
2. **A partially reporting run** (one tier OpenRouter, one not) must record `cost_usd=None`, never the partial sum. *(Task 3: two counters, one reporting.)*
3. **A reported zero must not read as missing**: the free model's `cost == 0` is recorded as `0.0`, not `None`. *(Tasks 1 and 3.)*
4. **`settle --log` on a missing file** and **a settle pass with no model configured** are both setup errors: red message naming the file/setting, exit 1, nothing written. *(Task 4.)*
5. **A P1-era index and a P1-era log** must produce exactly today's board text (and today's JSON plus a `usage: null` field). *(Task 6.)*

---

### Task 1: The run's books — `alpha_desk_research/usage.py`

The counter moves out of the CLI (the graph needs it in Task 3), gets a `run_id` dedupe (the CLI attaches one handler at both the clients and the run config, so every event arrives twice), and picks up provider-reported cost. `format_tokens` moves with it so the board (Task 6) can print compact numbers without importing the CLI.

**Files:**
- Create: `alpha_desk_research/usage.py`
- Delete: `cli/stats_handler.py`
- Modify: `cli/run.py:34` (import), `cli/display.py:217-221` (`format_tokens` moves out; import it), and any other importer found by `grep -rn "format_tokens\|stats_handler" --include=*.py .`
- Test: `tests/test_usage_stats.py` (new)

**Interfaces:**
- Produces:
  - `StatsCallbackHandler` — as today (`on_llm_start`, `on_chat_model_start`, `on_llm_end`, `on_tool_start`, `get_stats`), plus dedupe and cost.
  - `get_stats() -> dict` — today's keys (`llm_calls`, `tool_calls`, `tokens_in`, `tokens_out`) plus `cost_usd: float | None` (None until some call reports one) and `cost_calls: int` (how many calls reported one).
  - `format_tokens(n: int) -> str` — unchanged behaviour (`999` → `"999"`, `12345` → `"12.3k"`).
- Consumes: nothing (foundation task).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_stats.py
"""The usage counter: one handler attached twice counts one call once, and cost
is recorded only when the provider reported one."""
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from alpha_desk_research.usage import StatsCallbackHandler, format_tokens


def _end(handler, run_id=None, tokens_in=0, tokens_out=0, cost="absent"):
    """Deliver one on_llm_end the way a chat model does."""
    metadata = {"input_tokens": tokens_in, "output_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out}
    response_metadata = {} if cost == "absent" else {"token_usage": {"cost": cost}}
    message = AIMessage(content="x", usage_metadata=metadata, response_metadata=response_metadata)
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]),
                       run_id=run_id or uuid4())


def test_one_handler_attached_twice_counts_a_call_once():
    handler = StatsCallbackHandler()
    llm = FakeListChatModel(responses=["hi"]).with_config(callbacks=[handler])
    llm.invoke("x", config={"callbacks": [handler]})
    assert handler.get_stats()["llm_calls"] == 1


def test_the_same_end_delivered_twice_counts_its_tokens_once():
    handler, run_id = StatsCallbackHandler(), uuid4()
    _end(handler, run_id, tokens_in=100, tokens_out=10, cost=0.5)
    _end(handler, run_id, tokens_in=100, tokens_out=10, cost=0.5)
    stats = handler.get_stats()
    assert (stats["tokens_in"], stats["tokens_out"]) == (100, 10)
    assert stats["cost_calls"] == 1 and stats["cost_usd"] == 0.5


def test_a_reported_cost_is_summed_and_counted():
    handler = StatsCallbackHandler()
    _end(handler, tokens_in=10, cost=0.25)
    _end(handler, tokens_in=10, cost=0.5)
    stats = handler.get_stats()
    assert (stats["cost_usd"], stats["cost_calls"], stats["llm_calls"]) == (0.75, 2, 0)


def test_a_provider_that_reports_no_cost_leaves_cost_none():
    handler = StatsCallbackHandler()
    _end(handler, tokens_in=10)
    assert handler.get_stats()["cost_usd"] is None
    assert handler.get_stats()["cost_calls"] == 0


def test_a_reported_zero_is_not_missing():
    handler = StatsCallbackHandler()
    _end(handler, tokens_in=10, cost=0)
    assert handler.get_stats()["cost_usd"] == 0.0
    assert handler.get_stats()["cost_calls"] == 1


def test_format_tokens_is_unchanged():
    assert (format_tokens(999), format_tokens(1000), format_tokens(12345)) == ("999", "1.0k", "12.3k")
```

(`llm_calls` is 0 in the cost tests because they deliver `on_llm_end` alone — the pairing of starts to ends is what Task 3's all-or-nothing rule reads.)

**Step 2:** `python3 -m pytest tests/test_usage_stats.py -q` → fails on the import.

- [ ] **Step 3: Implement**

Move the class verbatim, then:
- add `self._starts: set = set()`, `self._ends: set = set()`, `self._tools: set = set()` beside the existing lock, and guard each hook: `if run_id in self._starts: return` (inside the lock) before counting — `run_id` is the `kwargs.get("run_id")` every hook already receives.
- in `on_llm_end`, after the existing token block, read the provider's own cost and count it: `usage = (generation.message.response_metadata or {}).get("token_usage") or {}`; `cost = usage.get("cost")`; record `self._cost_usd += float(cost)` / `self._cost_calls += 1` when `isinstance(cost, (int, float)) and not isinstance(cost, bool)`.
- `get_stats()` returns the two new keys.
- move `format_tokens` from `cli/display.py`.

- [ ] **Step 4: Run** `python3 -m pytest tests/test_usage_stats.py tests/test_cli_display.py -q` and `ruff check . && python3 -m pytest tests/test_cli_headless.py -q` (the CLI path that attaches the handler twice).

- [ ] **Step 5: Commit** — `usage: one counter for a run's calls, tokens and provider-reported cost`.

---

### Task 2: Ask OpenRouter for its own accounting

**Files:**
- Modify: `alpha_desk_research/llm_clients/openai_client.py` (`ProviderSpec` at `:184-206`, `OPENAI_COMPATIBLE_PROVIDERS` at `:212-222`, `get_llm` at `:277-335`)
- Test: `tests/test_openai_compatible_provider.py`

**Interfaces:**
- Produces: `ProviderSpec.extra_body: dict | None = None` — merged into the client's `extra_body` in `get_llm()`, the user's own `extra_body` (when one was passed through) winning per key; the `openrouter` row sets `{"usage": {"include": True}}`.

- [ ] **Step 1: Write the failing test**

```python
def test_the_openrouter_client_asks_for_cost_accounting(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm = create_tier_client(_config("openrouter"), "quick").get_llm()
    assert llm.extra_body == {"usage": {"include": True}}

def test_a_provider_without_a_spec_extra_body_sends_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert create_tier_client(_config("openai"), "quick").get_llm().extra_body is None
```

(`_config` builds a minimal config the way the file's existing tests do; reuse their helper.)

- [ ] **Step 2:** run → fails.
- [ ] **Step 3:** add the field, the row, and the merge in `get_llm()` (after the base-URL/key block, before the passthrough loop).
- [ ] **Step 4:** `python3 -m pytest tests/test_openai_compatible_provider.py tests/test_capabilities.py -q`, `ruff check .`.
- [ ] **Step 5: Commit** — `openrouter: ask for usage accounting so the run's cost is the provider's own number`.

---

### Task 3: Record the run's usage

**Files:**
- Modify: `alpha_desk_research/memory/run_index.py` (`RunRecord`), `alpha_desk_research/graph/trading_graph.py` (`__init__` at `:74-83`, `record_decision` at `:377-399`)
- Test: `tests/test_run_index.py` (it already has the `_graph(tmp_path)` helper that drives `record_decision` with real `__init__`)

**Interfaces:**
- Produces: `RunRecord.tokens_in: int = 0`, `RunRecord.tokens_out: int = 0`, `RunRecord.cost_usd: float | None = None`.
- Produces (graph): the counter it records from — the first callback exposing `get_stats`, else its own `StatsCallbackHandler` appended to `self.callbacks` in `__init__` *before* the clients are built, so the MCP path (which passes none) records too.
- Produces (graph): `_usage_since_last_record() -> dict` — the delta since the previous record, so a graph reused across cells (backtest) or jobs records each run's own bill, not the counter's running total; `cost_usd` is the delta only when the calls *this run* made all reported one.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_record_round_trips_the_usage_fields(tmp_path): ...
def test_a_record_with_no_usage_loads_with_zeros_and_none(tmp_path): ...   # the P1-era line
def test_the_graph_records_the_callers_counts(tmp_path):
    # callbacks=[FakeCounter(tokens_in=120, tokens_out=30, cost_usd=0.5, cost_calls=3, llm_calls=3)]
    # -> the appended line carries them
def test_the_graphs_own_counter_is_used_when_the_caller_keeps_none(tmp_path):
    # the graph's callbacks gained a StatsCallbackHandler; the line carries zeros (nothing ran)
def test_a_second_record_carries_only_what_happened_between_them(tmp_path):
    # counter grows after the first record -> the second line carries the delta
def test_a_partially_reporting_run_records_no_cost(tmp_path):
    # llm_calls=4, cost_calls=3 -> cost_usd is None, tokens still recorded
def test_a_reported_zero_cost_is_recorded_as_zero(tmp_path):
```

- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement the three fields and the graph side:
```python
# __init__, before the clients are built
self.callbacks = list(callbacks or [])
if not any(callable(getattr(cb, "get_stats", None)) for cb in self.callbacks):
    # A caller that keeps no books still gets its runs' usage recorded; ours is
    # attached the same way theirs would be.
    self.callbacks.append(StatsCallbackHandler())
self._usage_snapshot: dict = {}   # stats as of the last record_decision
```
and in `record_decision`, `usage = self._usage_since_last_record()` feeding `RunRecord(..., tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"], cost_usd=usage["cost_usd"])`.
- [ ] **Step 4:** `python3 -m pytest tests/test_run_index.py tests/test_graph_end_to_end.py tests/test_backtest.py -q`, `ruff check .`.
- [ ] **Step 5: Commit** — `runs: index what a run spent, from the caller's counter or the graph's own`.

---

### Task 4: Settle from a scheduler — `settle_configured` + `alpha-desk settle`

**Files:**
- Modify: `alpha_desk_research/memory/settlement.py`, `cli/main.py` (a command beside `scoreboard` at `:203`), `README.md`
- Test: `tests/test_memory_settlement.py` (new), `tests/test_cli_commands.py`

**Interfaces:**
- Produces: `settle_configured(config: dict, ticker: str | None = None) -> Settlement` — builds the log and the reflector the way a run does (`Reflector(create_tier_client(config, "quick").get_llm())`; the quick tier answers reflections, `trading_graph.py:110`), then `settle_pending` for one ticker or `settle_all_pending` for all. A `ValueError` from a missing key/model propagates — it is a setup error, not a settlement failure.
- Produces: `as_dict(result: Settlement, entries: list[dict] | None = None) -> dict` — `{"settled": [{"ticker", "date", "raw", "alpha"}], "failed": [{"ticker", "date", "reason"}]}`, the raw/alpha read from the log's entries by `(ticker, date)` (`entry.get("raw")`, `entry.get("alpha")`, falling back to `"n/a"`). This is the shape both the CLI's `--json` and the MCP tool hand out.
- Produces (CLI): `alpha-desk settle [--ticker T] [--log PATH] [--json]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory_settlement.py
def test_settle_configured_builds_the_quick_tier_and_settles(monkeypatch, tmp_path):
    # patch alpha_desk_research.memory.settlement.create_tier_client -> a stub
    # recording the tier and whose get_llm() returns a sentinel; patch
    # ...settlement.fetch_returns -> (0.012, 0.004, 5, "2026-01-09");
    # seed a log with a pending decision; assert the reflector was the sentinel's
    # and the entry now carries the alpha, and the tier asked for was "quick"

def test_nothing_due_asks_the_model_nothing(monkeypatch, tmp_path):
    # patch ...settlement.fetch_returns -> (None, None, 0, None): the window has
    # not traded. The reflector must never be called and the pass must be empty.

# tests/test_cli_commands.py — the command imports `settle_configured` and
# `as_dict` at module top (cli/main.py), so the tests patch `cli.main.settle_configured`
def test_settle_prints_what_it_settled(...)             # Settlement with one (CAT, 2026-01-05)
def test_settle_json_is_the_shared_shape(...)           # as_dict over the same Settlement
def test_settle_on_a_missing_log_is_a_setup_error(...)  # --log nope.md: exit 1, "No memory log at"
def test_settle_reports_a_missing_model_as_a_setup_error(...)
    # settle_configured raises ValueError("... OPENROUTER_API_KEY ..."): exit 1, no traceback
```

- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement the helper, the serializer, and the command (mirror `scoreboard`'s option and `--log` handling exactly, including the missing-file error at `cli/main.py:231-235`; do *not* touch `run_index_path` — settle reads only the log). Text output: one `settled` line per decision with `raw`/`alpha`, one `failed` line per failure, and when neither happened a line saying nothing was due.
- [ ] **Step 4:** `python3 -m pytest tests/test_memory_settlement.py tests/test_cli_commands.py -q`, `ruff check .`. README: add `settle` to the CLI section beside `scoreboard`.
- [ ] **Step 5: Commit** — `settle: a scheduler's command — outcomes and lessons with no analysis run`.

---

### Task 5: The same pass over MCP

**Files:**
- Modify: `mcp_server/server.py` (a `get_settle` beside `get_scoreboard` at `:215`, a `tool_*` wrapper beside `tool_get_scoreboard`, registration in `build_app` at `:349-356`, and the docstring at `:308`: "the seven research tools" → "the eight research tools")
- Test: `tests/test_mcp_contract.py`

**Interfaces:**
- Produces: the MCP tool `settle_decisions(ticker: str | None = None) -> str` — the CLI's `--json` string, from the same helper and the same config resolution as `get_scoreboard` (`build_default_config`); errors come back as `ERROR: …` through the file's existing `_err`.

- [ ] **Step 1: Write the failing test** — seed a temp log with a due decision; patch `alpha_desk_research.memory.settlement.settle_configured` (imported inside `get_settle` the way `get_scoreboard` imports its own) to return a fixed `Settlement` and let the real `as_dict` read the log; call `tool_settle_decisions()` and `alpha-desk settle --json` (with `cli.main.settle_configured` patched to the same value) over the same log, and assert the two strings parse to the same dict. A missing model must come back as a string starting `ERROR:` naming the setting (patch the helper to raise `ValueError`), and `build_app` must register the tool.
- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement, mirroring `tool_get_scoreboard`'s exact pattern (read it before writing).
- [ ] **Step 4:** `python3 -m pytest tests/test_mcp_contract.py -q`, `ruff check .`.
- [ ] **Step 5: Commit** — `mcp: settle_decisions, the same JSON the CLI prints`.

---

### Task 6: The board shows the bill

**Files:**
- Modify: `alpha_desk_research/scoreboard.py` (`GroupScore` at `:54`, `build_scoreboard` at `:109`, `render` at `:182`, `_row_line` at `:236`, `_rows`/`_score_row` at `:346-382`), `README.md`, `CHANGELOG.md`
- Test: `tests/test_scoreboard.py`

**Interfaces:**
- Produces: `Usage(decisions: int, tokens_in: int, tokens_out: int, cost_reporting: int, cost_usd: float | None)` — `decisions` is how many of the row's decisions had a record carrying usage; the cost is a total *and* `cost_reporting` says over how many of them (`0` → `cost_usd is None`). Frozen dataclass, like the rest of the module's data.
- Produces: `GroupScore.usage: Usage | None` — `None` when nothing in the row has usage. `as_dict` picks it up by construction (`asdict`); a row with no usage serializes `"usage": null`.
- `render()` appends `, tokens 117.1k in/23.4k out per decision (n=1)` and, only when every usage-carrying record in the row reported a cost, `, cost $0.42 per decision (n=1)` — via `format_tokens` imported from `alpha_desk_research.usage`; the mean is over the usage-carrying decisions, and a row with no usage prints exactly today's line.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_row_says_what_a_decision_cost(): ...            # means over usage-carrying decisions
def test_a_row_counts_only_the_decisions_whose_runs_recorded_usage(): ...   # n=2 of 3 decisions
def test_cost_is_printed_only_when_every_recorded_run_reported_one(): ...
def test_a_reported_zero_cost_is_printed_as_zero(): ...
def test_a_row_with_no_usage_reads_exactly_as_before():     # hardcode today's line, byte for byte
def test_an_old_index_serializes_usage_null(): ...
```

- [ ] **Step 2:** run → fail.
- [ ] **Step 3:** implement: `build_scoreboard` builds a `usage_of(entry)` from the same `by_key` mapping the labels use, and `_rows`/`_score_row` take it alongside `key`.
- [ ] **Step 4:** `python3 -m pytest tests/test_scoreboard.py tests/test_cli_commands.py tests/test_reporting.py -q`, `ruff check .`. README: the board's columns. CHANGELOG: one entry for the slice.
- [ ] **Step 5: Commit** — `scoreboard: what a row's decisions cost, where the provider said so`.

---

## Finish

- [ ] **Live proof** (controller, not a subagent): a real run on the free OpenRouter model, then `alpha-desk settle` and `alpha-desk scoreboard` over that log; confirm the index line carries `tokens_in`/`tokens_out` > 0 and `cost_usd: 0.0`, the live view's counter no longer doubles, and the board prints the usage line. Record the actual output in the handoff.
- [ ] **Whole-branch review** on the most capable model, then merge to `main` and push.
- [ ] `python3 -m pytest -q` whole suite green, `ruff check .` clean, README test count updated if it moved.
