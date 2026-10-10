# Settle, and what a run cost: closing the scoreboard's loop

## Problem

P1 shipped the scoreboard: it reads the memory log and the run index and reports
hit rate and mean alpha per rating, model, provider and analyst set. Two gaps
stop it from closing the loop on its own, and a third makes any number it is
about to gain untrustworthy.

1. **Settling has no surface.** `settle_all_pending` exists
   (`memory/settlement.py:158`), its docstring says it is for a scheduled job, and
   the graph already calls it beside the analysts (`trading_graph.py:340`) — but
   there is no CLI command and no MCP tool. Resolving the NVDA decision made
   during P1's post-merge verification took a hand-written Python snippet
   (`AlphaDeskGraph().settle_all_pending()`). A cron entry cannot shell out to
   that, so decisions stay pending until the next analysis of the same ticker —
   and their outcomes never reach the board.
2. **A run records who decided, not what it cost.** `RunRecord`
   (`memory/run_index.py:40-49`) carries the settings allowlist, so the board can
   attribute a decision to a model — but not say what that model's decision cost.
   The slice-2 cost matrix therefore re-runs a paid grid to obtain numbers the
   run itself already produced. `StatsCallbackHandler` (`cli/stats_handler.py`)
   counts them on every CLI run; `benchmarks/run_baseline.py:89` persists
   `by_model`; the index keeps none of it.
3. **The counter counts every call twice.** `cli/run.py` attaches the same
   `stats_handler` instance to the LLM clients (`cli/run.py:127`) and to the run
   config (`cli/run.py:253`). Measured: one `invoke()` with both attachments
   fires `on_llm_start`/`on_llm_end` twice, so `llm_calls` reads 2 and both token
   counts are doubled. Whatever the index records must not inherit this.

Evidence for the shape of the fix: an OpenRouter response carries
`response_metadata["token_usage"]["cost"]` (and `cost_details`) when the request
sets `extra_body={"usage": {"include": True}}`; a free model reports exactly `0`
— reported zero, not missing. Providers that do not report cost return nothing.

## Constraints from the existing code

- The run index is instrumentation: a failed append never fails the run
  (`run_index.py:64-79`), and a malformed line is skipped and counted
  (`run_index.py:96-109`). New fields must stay additive so lines written by P1
  still load.
- `RunRecord.load` filters unknown keys and defaults missing ones
  (`run_index.py:106`), so old lines load without new fields and new lines load
  in an old process.
- `cli/display.py:401-408` reads `get_stats()["tokens_in"|"tokens_out"]`; the
  handler's public shape is therefore fixed and only additive.
- The analyzer path already requires an unattended setup (provider, models,
  rounds, language — `cli/selections.py:unattended_gaps`); a scheduled settle
  needs the *quick* tier only, because a settled entry is written together with
  its reflection, and a reflection failure leaves the entry pending
  (`settlement.py:138-151`).
- The scoreboard's render must stay unchanged when the data it would show is
  absent — the same rule P1 applied to the HTML report's track-record section.
- `tests/test_backtest.py:199-229` pins the scoring rule; nothing in this slice
  changes it.
- Three pre-existing macOS-only failures (`errno.EDEADLOCK`,
  `dataflows/files.py:82`) are the accepted baseline; they belong to P2.

## Non-goals

- No price table, and no cost for providers that do not report one. The field is
  the provider's own number or nothing.
- No per-model split inside a single run's record; a run has one quick and one
  deep tier, and the record carries their sum.
- No scheduler (cron/GitHub Action) in this slice — that is the slice after this
  one; this slice makes the command it will call.
- No changes to the scoring rule, the memory-log format, or the sample floor.
- No statistical claims; the non-independence caveat stays as P1 wrote it.

## Design

### 1. Usage accounting (`alpha_desk_research/usage.py`, moved)

`StatsCallbackHandler` moves out of `cli/` — the graph must be able to construct
it, and `alpha_desk_research` must never import `cli`. Its three LLM hooks take
`run_id` explicitly and ignore a `run_id` they have already counted, which is
what makes an instance safe to attach on both the LLM clients and the run config.
`cli/stats_handler.py` is deleted and its importers updated (`cli/run.py`,
`cli/display.py` takes the handler duck-typed and is unchanged).

Cost: `on_llm_end` reads `token_usage["cost"]` from
`response_metadata` when it is a number; `get_stats()` gains `cost_usd` and
`cost_calls`, where `cost_calls` counts LLM calls that reported a cost and
`llm_calls` counts calls overall. The record writes a cost only when
`cost_calls == llm_calls` and `llm_calls > 0`: a partially reported sum would be
a partial number presented as the run's cost. This mirrors how the scoreboard
already declines to print a rate below its sample floor.

### 2. Provider plumbing (`llm_clients/openai_client.py`)

`ProviderSpec` gains `extra_body: dict | None = None`, merged into the
`ChatOpenAI` kwargs in `get_llm` (the same place `use_responses_api` is decided).
The `openrouter` row sets `{"usage": {"include": True}}`. Providers without a
row field send exactly what they send today; the `extra_body` kwarg is not in
`_PASSTHROUGH_KWARGS`, so user config cannot collide with it.

### 3. The record (`memory/run_index.py`)

`RunRecord` gains three fields, all with defaults that keep old lines loadable
and new lines readable by an old process:

```python
tokens_in: int = 0
tokens_out: int = 0
cost_usd: float | None = None   # provider-reported; None when it did not report
```

### 4. The graph owns the numbers (`graph/trading_graph.py`)

`AlphaDeskGraph.__init__` keeps a usage counter: the first callback that exposes
`get_stats()` (the CLI's handler, when it passed one), else a fresh
`StatsCallbackHandler` of its own — attached to the LLM clients either way, so
the MCP worker and backtest sweeps record usage with no caller change.
`record_decision` writes `tokens_in`, `tokens_out` and `cost_usd` into the
`RunRecord` it already appends.

### 5. Settling on demand (`memory/settlement.py`, `cli/main.py`, `mcp_server/server.py`)

```python
def settle_configured(config: dict, ticker: str | None = None) -> Settlement
```

A module-level helper beside `settle_all_pending`: it builds the memory log and
the quick-tier client for `config` (`create_tier_client(config, "quick")` — the
same factory the graph uses), wraps the pass in `run_config(config)` so the
price reads use the configured vendors and cache, and settles `ticker`'s
decisions or every ticker's. No graph, no analysts, no report.

CLI: `alpha-desk settle [--ticker T]... [--log PATH] [--json]`. `--log` points
the pass at another log (a backtest sweep's), exactly as `scoreboard --log`
already does. The text output lists what settled — ticker, date, raw and alpha —
and what failed with its reason; `--json` emits the same data as
`{"settled": [...], "failed": [...]}`. Nothing due is a success ("0 decisions
settle yet; their windows have not traded"). A config that cannot call a model
is an error with the missing setting named, not a traceback.

MCP: `settle_decisions(ticker: str | None = None)` returning the same JSON,
built from `build_default_config()` the way `get_scoreboard` is
(`mcp_server/server.py:209-237`), plus its `tool_*` wrapper returning
`"ERROR: ..."` instead of raising.

### 6. The board shows the bill (`scoreboard.py`)

A group row gains usage aggregated over the records its decisions map to:

- **tokens**: mean `(tokens_in, tokens_out)` per decision, printed whenever any
  matched record has tokens above zero;
- **cost**: mean `cost_usd` per decision, printed only over records that
  reported one, with the count — `$0.42 (n=2 of 5)` — and omitted entirely when
  none did;
- **coverage** line already reports decisions with no run record; a decision
  whose record has no cost is part of that same honesty, reported in the cost
  cell's own `(n=... of ...)`.

`as_dict` gains `usage` per group and overall. The render is byte-identical to
today for an index with no usage fields — old indexes and their tests keep their
output.

## Error handling

| Case | Behaviour |
|---|---|
| No model configured for `settle` | Red error naming the missing setting; exit 1; nothing written |
| Reflection fails (provider down) | Entry stays pending; listed as failed with the reason; exit 0 (the command ran) |
| Prices unavailable | Same: listed as failed, entry stays pending |
| Nothing due | Success, says so |
| Provider reports no cost | `cost_usd: null`; board prints tokens only |
| Some calls reported cost, some did not | `cost_usd: null` (all-or-nothing) |
| `--log` pointing at a missing file | Red error, exit 1 — the same rule `scoreboard --log` already enforces |
| Index line from P1 (no usage fields) | Loads; board unchanged for those rows |

## Testing

- `tests/test_usage_stats.py`: attaching one handler twice counts one call once,
  and one cost once (the regression this slice exists to prevent); cost is
  reported only when every call reported one; tokens matched to the fake
  provider's numbers.
- `tests/test_openai_compatible_provider.py` (existing file): the openrouter spec
  sends `extra_body={"usage": {"include": True}}`; a provider without the field
  sends none.
- `tests/test_run_index.py`: round-trip of the new fields; a P1 line loads with
  zeros and `None`; an unknown field is still dropped.
- `tests/test_graph_end_to_end.py`: the graph records the caller's counts when it
  was given a counter, and its own when it was not.
- `tests/test_memory_settlement.py` (new): `settle_configured` settles a due entry with
  a fake reflector and leaves an undated/window-open one pending; a tidy-up
  entry's failure is reported, not raised.
- `tests/test_cli_commands.py`: `settle` parses `--ticker`/`--log`/`--json` and
  does not run an analysis (same guard as `scoreboard`); text output names a
  settled row; `--json` parses.
- `tests/test_mcp_contract.py`: `tool_settle_decisions()` returns the same JSON
  as `--json` for the same log, and returns `"ERROR: ..."` for a bad config.
- `tests/test_scoreboard.py`: usage aggregation per group (mean tokens, cost over
  reporting records only, `null` when partial), and the render guard (no usage →
  today's bytes).
- Live proof: one real run (free OpenRouter model) whose index line carries
  tokens and `cost_usd == 0.0`; `alpha-desk settle` on it once its window has
  traded; the board showing tokens/cost for the attributed row.

## Acceptance criteria

1. `alpha-desk settle` settles due decisions end to end with no analysis and no
   report written; `--json` is machine readable; `--log PATH` settles another
   log; a missing model is an error naming the setting, not a traceback.
2. MCP `settle_decisions` returns the same numbers as `--json` for the same log.
3. After a CLI run and after an MCP run, the index line carries `tokens_in`,
   `tokens_out` and `cost_usd`; on OpenRouter these equal the provider's own
   numbers, and `cost_usd` is `0.0` for a free model, `null` when no call
   reported a cost.
4. One LLM call counts once no matter how many times the handler is attached
   (regression test), so no recorded number is doubled.
5. `alpha-desk scoreboard` prints tokens per row when the index has them and cost
   only over reporting records; with a P1-only index the output is unchanged.
6. `ruff check .` clean; the suite passes with only the three known macOS
   `EDEADLOCK` failures (P2's).
7. `README.md` and `CHANGELOG.md` describe the command and the new fields.

## Risks and open questions

- **Cost semantics**: `null` (not reported) and `0.0` (reported zero — free
  models) are different facts and the board must not conflate them. Pinned by
  test.
- **Attach-twice regressions**: the dedupe key is `run_id`; if a future
  langchain version stops passing it, the counter silently doubles again. The
  handler counts calls whose `run_id` is `None` unconditionally, and the
  regression test pins the double-attach case.
- **Partial cost**: any provider mix where one tier reports cost and the other
  does not yields `null`, which is honest but less useful; P3 can add a price
  table if the desk ever runs non-reporting providers as its default.
- **Index growth**: three more fields per line, still one line per run, never
  pruned (P1's decision).
