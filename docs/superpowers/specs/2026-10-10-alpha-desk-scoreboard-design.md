# Scoreboard: measuring whether the desk's calls are any good

Status: design approved in chat 2026-10-10; spec awaiting review.
Scope: sub-project P1 of three (programme decomposition below). Implementation
plan comes after this spec is approved.

## Problem

Alpha Desk already records every decision and, days later, what it returned:
`memory/settlement.py` fills `raw%`, `alpha%`, `5d` and `resolved:DATE` into the
append-only log, and `backtest.py:summarize()` aggregates a log into
`BacktestSummary{resolved, pending, by_rating{count, hit_rate, mean_alpha},
unscored, holding}`.

Nothing reads those numbers for the desk you actually run. The live
`~/.alpha-desk/memory/trading_memory.md` is only ever read back by the agents
themselves as lessons. There is no answer to:

- is the desk's hit rate better than a coin flip over the decisions it made?
- does the cheap/free model produce worse calls than the expensive one?
- does a four-analyst run beat a two-analyst run?
- how many runs are wasted (no tradeable rating) rather than merely wrong?

The memory log cannot answer the last two at all: the tag line is
`[date | ticker | rating | raw% | alpha% | Nd | resolved:DATE]` and settlement
**rebuilds that tag from scratch** (`memory/log.py:182-184`, `_resolved_tag`), so
model, provider, analyst set and round counts are not recorded anywhere a
settlement survives. Attribution is the missing piece, and it must not go in the
tag line.

## Constraints from the existing code

- The log format is pinned by `tests/test_memory_log.py` (tag shapes, `:+.1%`
  formatting, 6-field legacy form, idempotency guard) and
  `tests/test_memory_pointintime.py`. Changing the tag line is out of scope.
- `backtest.py:summarize()` already owns the scoring rule and is pinned by
  `tests/test_backtest.py:199-229`: hit = alpha sign agrees with the rating's
  direction (`_DIRECTION`), `Hold` claims no direction so it has no hit rate,
  `REVIEW` entries are excluded and reported as `unscored`, pending entries are
  excluded from averages. The scoreboard MUST NOT fork this rule.
- Backtest runs redirect `memory_log_path` into their own run directory
  (`backtest.py:151-152`), so anything reading `config["memory_log_path"]` scores
  the right log in either mode without special-casing.
- `run_settings()` (`graph/trading_graph.py:262-275`) is the run's identity
  record and is already asserted to contain no endpoints or paths
  (`tests/test_reporting.py:79-93`).
- `record_decision()` (`graph/trading_graph.py:358-370`) is where a finished run
  lands, and it returns early when there is no decision — the wasted-run case.

## Programme decomposition

Three independent sub-projects, each with its own spec → plan → implementation.
This document designs P1 and records the rest so scope is not lost.

| # | Sub-project | Scope |
|---|---|---|
| **P1** | **Scoreboard** (this spec) | run index, scoreboard aggregation, CLI/MCP/report-card surfaces, per-config cost matrix |
| P2 | Decision integrity | `dataflows/files.py:82` references `errno.EDEADLOCK`, absent on macOS, so the Windows retry path raises `AttributeError` off-Windows (3 failures); the structured-output path (`agents/structured.py:76-81` swallows every exception, single attempt) and `portfolio_manager.py:84-90` fall back to free text → `parse_rating` → `REVIEW` with no validation or retry; CI gains formatter, coverage and audit gates |
| P3 | Data edge | vendor slices behind `router.py`'s static `TOOLS_CATEGORIES`/`VENDOR_METHODS`: earnings dates, estimates/surprise, short interest/float, options (spike first), 13F. Each respects the pins in `tests/test_graph_end_to_end.py:142-146` and `tests/test_tool_date_enforcement.py` |

P1 first: it reads data already on disk, risks no live run, and every later
"this vendor/model/analyst is better" claim depends on it.

## Non-goals

- No p-values, significance tests, or statistical claims. Decisions made on the
  same date share market beta, so cells are not independent and the scoreboard
  says so in its output.
- No portfolio-level P&L, no trade simulation beyond what `backtest.py` already
  does.
- No scheduler, watcher, notification or dashboard (that is the excluded §3).
- No change to the memory log tag format, and no new fields in it.
- No new data vendors (P3).

## Design

### 1. Run index (new: `alpha_desk_research/memory/run_index.py`)

An append-only JSONL sidecar beside the memory log, one object per **attempt**
(not per decision), so runs that produce nothing are counted rather than lost.

```json
{"ts": "2026-10-10T21:11:51+00:00", "ticker": "CAT", "trade_date": "2026-10-09",
 "rating": "Buy", "asset_type": "stock", "signature": "analysts=market,news|debate=1|...",
 "settings": {"version": "1.0.0", "llm_provider": "openrouter", "deep_think_llm": "...",
              "quick_think_llm": "...", "analysts": ["market", "news"],
              "max_debate_rounds": 1, "max_risk_discuss_rounds": 1,
              "output_language": "English", "data_vendors": {}, "tool_vendors": {}}}
```

- Path defaults to `Path(config["memory_log_path"]).with_name("runs.jsonl")`, so
  a backtest run writes its own index beside its own log. The new
  `run_index_path` config key is registered in `default_config.py`'s
  `ENV_OVERRIDES` map so `ALPHA_DESK_RUN_INDEX_PATH` works like every other
  setting (`default_config.py:33`).
- `settings` is exactly `run_settings()` — never paths or keys.
- `rating` is `run_rating(final_state)`, so `"REVIEW"` is recorded literally.
- Written under the existing `dataflows/files.locked()` for the append.
- **A failure to write the index never fails the run**: warn and continue. The
  index is instrumentation.
- Reading tolerates the world: a malformed line is skipped and counted, a missing
  file is an empty index.

### 2. Scoring rule (move, don't fork)

Move `_alpha`, `_DIRECTION` and `RatingScore` out of `backtest.py` into
`alpha_desk_research/memory/scoring.py`, and add the grouping primitive there:

```python
def score_entries(entries: list[dict], key: Callable[[dict], str | None] | None = None) -> dict[str | None, RatingScore]
def alpha_of(entry: dict) -> float | None
```

`backtest.summarize()` keeps its signature and output and delegates to
`score_entries` for the rating grouping. `BacktestResult`/`BacktestSummary` stay
in `backtest.py` (tests import `BacktestResult` from there). No external module
imports `RatingScore` or `_DIRECTION` today, so the cutover is complete and adds
no re-export shim.

### 3. Aggregation (new: `alpha_desk_research/scoreboard.py`)

```python
@dataclass
class Scoreboard:
    groups: dict[str, list[GroupScore]]     # dimension -> rows
    overall: GroupScore
    coverage: Coverage                      # see below
    window: str                             # the holding window the outcomes used

@dataclass
class GroupScore:
    label: str; count: int; resolved: int; pending: int; unscored: int
    hit_rate: float | None; mean_alpha: float | None; first: str; last: str

@dataclass
class Coverage:
    runs_without_entry: int; entries_without_record: int; malformed_lines: int
    index_present: bool
```

- Input: `(log entries, run records)`, joined on `(trade_date, ticker)`.
- Scored set is exactly `summarize()`'s: settled, alpha parseable, rating not
  `REVIEW`. `REVIEW` and pending are counted per row, never averaged.
- Dimensions: `rating`, `model` (`deep_think_llm`/`quick_think_llm`),
  `provider`, `analysts` (comma-joined tuple), `rounds`, `ticker`, `period`
  (calendar month of `trade_date`), `horizon` (`holding`).
- `hit_rate` is `None` in two distinct cases, and the renderer must distinguish
  them: a rating with no direction (`Hold`) prints `no direction claimed`
  whatever its `n`, and a directional rating below `min_sample` (default 10)
  prints `n=7, too few to rate` rather than a rate. `mean_alpha` is always shown
  with its `n`.
- Attribution never guesses: entries with no run record are grouped under
  `unknown (no run record)` and surfaced in `coverage`.
- Output text names the sample: counts, the date range, and the sentence that
  same-date decisions are not independent.

### 4. Surfaces

- **CLI**: `alpha-desk scoreboard [--ticker T] [--since D] [--until D]
  [--group-by rating|model|provider|analysts|rounds|ticker|period|horizon]
  [--min-sample N] [--log PATH] [--json]`. `--since`/`--until` filter on the
  entry's `trade_date` (inclusive). Default output is `overall` plus the
  `rating` dimension; `--group-by` selects one dimension per flag and may be
  repeated. `--log PATH` scores any log file — including a backtest run's —
  which is how one config is compared with another without new aggregation code.
- **MCP**: read-only `get_scoreboard(ticker=None, since=None, group_by=None)` in
  `mcp_server/server.py`, returning the `Scoreboard` as JSON with the same keys
  as `--json` (`groups`, `overall`, `coverage`, `window`); `group_by=None` means
  `overall` plus `rating`, matching the CLI default. Update
  `tests/test_mcp_contract.py` (it pins the current tool set).
- **Report card**: `reporting.write_report_tree(..., scoreboard=None)` and
  `report_html.render_report(..., scoreboard=None)` gain an optional section
  ("Track record") listing the ticker's rows and the overall row. `None` leaves
  every current page byte-identical. CLI and MCP report paths pass the computed
  scoreboard (cheap: it is a read of the same memory log).

### 5. Hook

In `record_decision()` (`graph/trading_graph.py:358`), write the run-index
record **before** the `if not decision: return` early exit, so a run with no
decision is still counted. Keep `_log_state` and `store_decision` untouched.

### 6. Per-config cost matrix (slice 2, separable)

- `benchmarks/run_baseline.py` gains `--config <json>` (overrides merged over
  `DEFAULT_CONFIG`) and the envelope records the config that produced it. No
  test pins that envelope, so this is additive.
- `benchmarks/run_matrix.py` drives one baseline run per config × date, then
  renders cost/latency (`wall_total_s`, `tokens.total`, `ok`) per config.
- Quality per config comes from P1's own surfaces: run the backtest CLI per
  config, then `alpha-desk scoreboard --log <that run's log>`. No new
  aggregation code, and the comparison is honest about repeated runs.

## Error handling

| Situation | Behaviour |
|---|---|
| No run index (existing installs) | Scoreboard groups only what the log knows; coverage says the index is absent |
| Index line malformed | Skipped, counted in coverage, never fatal |
| Log entry with no run record | Grouped under `unknown (no run record)` |
| Run record with no log entry (deleted, or no decision) | Counted as a wasted/no-decision run, excluded from scoring |
| Index write fails | Warning; the run continues |
| Sample below `min_sample` | No hit rate shown; counts and mean alpha still shown |
| Log pruned by `memory_log_max_entries` | Old settled entries disappear from the log while the index keeps the history; coverage reports the mismatch |

## Testing

- `tests/test_run_index.py`: append/load round-trip; malformed line skipped and
  counted; concurrent appends under `locked()`; `settings` carries no path or
  secret; a run with no decision still writes a record (regression for the hook
  placement); a failing write does not raise.
- `tests/test_scoreboard.py`: the **parity test** — on one seeded log, the
  rating grouping equals `backtest.summarize(path)` field for field, which is
  the proof the rule was not forked; `REVIEW` unscored, never averaged; pending
  excluded; every dimension; the unattributed bucket; unknown tickers; empty log;
  `min_sample` suppression.
- `tests/test_backtest.py` stays unchanged and green — behaviour did not move
  when the code did.
- `tests/test_cli_commands.py`: `scoreboard` over a seeded log; `--json` shape;
  `--log PATH`.
- `tests/test_mcp_contract.py`: the new read-only tool added to the pinned set.
- `tests/test_reporting.py`/`tests/test_report_html.py`: `scoreboard=None`
  produces the current page; with data the section renders.
- Smoke: a real CLI run appends exactly one index line; `alpha-desk scoreboard`
  then prints it.

## Acceptance criteria

1. After any run, `runs.jsonl` beside the memory log has one line per attempt,
   including runs that ended `REVIEW` or with no decision.
2. `alpha-desk scoreboard` prints overall and per-rating rows with `n`, hit rate,
   mean alpha, window and date range, and its rating numbers are **identical** to
   `backtest.summarize()` on the same log (parity test).
3. `--log PATH` scores a backtest run's log; two configs compare by pointing the
   command at each run's log.
4. MCP `get_scoreboard` returns the same numbers as the CLI for the same log.
5. The HTML report gains a track-record section when data exists, and is
   unchanged when it does not.
6. Below the sample floor the output says so instead of printing a hit rate.
7. `ruff check .` clean; the suite passes with only the three known macOS
   `EDEADLOCK` failures (which P2 fixes).

## Risks and open questions

- **Sample floor**: default 10 decisions per row. Yours to change; the renderer
  must never quietly present a rate below it.
- **Non-independence**: same-date cells share beta. Stated in the output; no
  inference is drawn beyond counting.
- **Index growth**: one line per run, never pruned. Negligible, and pruning
  history would defeat the purpose.
- **Index/log drift** (hand-edited logs, deleted runs) is reported by coverage
  rather than smoothed over.
- **Slice 2 cost**: a config × grid matrix is many paid runs. It is a separate,
  explicitly invoked command for that reason.
