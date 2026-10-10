# Scoreboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the desk's call quality measurable — record what each run was (model, provider, analysts, rounds, rating) beside the memory log, and aggregate settled outcomes into a scoreboard that reuses the existing scoring rule rather than forking it.

**Architecture:** A new append-only JSONL sidecar (`memory/run_index.py`) records one line per run attempt, including runs that produced no decision. The scoring rule moves out of `backtest.py` into `memory/scoring.py` so a new `scoreboard.py` and the existing `summarize()` share one implementation. Three read-only surfaces expose the numbers: a CLI command, an MCP tool, and an optional section in the HTML report.

**Tech Stack:** Python 3.11+, dataclasses, `typer` (CLI), `fastmcp` (MCP), `pytest` (tests), `rich` (CLI output). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-10-alpha-desk-scoreboard-design.md`

## Global Constraints

- The memory-log tag format does **not** change and gains no new fields: settlement rebuilds the tag from scratch (`memory/log.py:182-184`), so an inline field would be dropped. Formats are pinned by `tests/test_memory_log.py` and `tests/test_memory_pointintime.py`.
- The run index records exactly `run_settings()` values — never endpoints, paths, or keys (mirror the assertions in `tests/test_reporting.py:79-93`).
- A failure to write the run index never fails a run: warn and continue.
- Scoring semantics are fixed: hit = the alpha sign agrees with the rating's direction (`Buy`/`Overweight` +1, `Underweight`/`Sell` −1, `Hold` 0); `Hold` has no hit rate; `REVIEW` entries are counted as unscored and never averaged; pending entries are excluded from averages.
- `min_sample` defaults to 10; at or below it no hit rate is printed. `Hold` prints `no direction claimed` regardless of `n`.
- Output states the sample (counts, date range) and that same-date decisions are not independent. No p-values, no significance claims.
- All tests run offline (`tests/conftest.py` refuses network) and must pass with `python3 -m pytest`. `ruff check .` stays clean.
- Python 3.11+; no new dependencies.

## Review Focus

Inputs and conditions the spec implies but that no obvious test covers — each gets a test in the task that owns the code:

1. A hand-edited log line whose rating is not one of the five tiers (`Strong Buy`) must appear as its own row with no hit rate, not crash and not vanish. *(Task 5)*
2. An index line that is unparseable JSON vs. one that parses but has no usable `settings`: the first is skipped and counted, the second counts the run and groups it under `unknown (no settings)`. *(Tasks 2 and 5)*
3. Two index lines for the same `(ticker, trade_date)` (a re-run) must not double-count: the last wins, the earlier is counted as a repeat. *(Task 5)*
4. A run recorded in the index with no log entry (crash before `store_decision`, or `--no-save`) is counted as a wasted run, never silently dropped. *(Task 5)*
5. An alpha string that is not a number (`n/a`, `+?`) is excluded from the means without raising. *(Task 1)*

---

### Task 1: Move the scoring rule into `memory/scoring.py`

**Files:**
- Create: `alpha_desk_research/memory/scoring.py`
- Modify: `alpha_desk_research/backtest.py` (scoring block at `:63-110`; `summarize` at `:184-215`)
- Test: `tests/test_scoring.py` (new); `tests/test_backtest.py` unchanged — staying green is the proof the rule did not move behaviourally

**Interfaces:**
- Produces: `alpha_of(entry: dict) -> float | None`; `DIRECTION: dict[str, int]`; `RatingScore(count: int, hit_rate: float | None, mean_alpha: float)`; `score_entries(entries: list[dict], key: Callable[[dict], str | None] | None = None) -> dict[str | None, RatingScore]`
- Consumes: nothing (foundation task)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scoring.py
from alpha_desk_research.memory.scoring import DIRECTION, RatingScore, alpha_of, score_entries

def _entry(rating, alpha, pending=False):
    return {"date": "2026-01-05", "ticker": "NVDA", "rating": rating,
            "pending": pending, "alpha": alpha, "holding": "5d"}

def test_a_bearish_call_that_fell_is_a_hit():
    assert score_entries([_entry("Sell", "-4.0%")])["Sell"].hit_rate == 1.0

def test_hold_has_no_hit_rate():
    assert score_entries([_entry("Hold", "+9.0%")])["Hold"].hit_rate is None

def test_review_and_pending_are_never_scored():
    assert score_entries([_entry("REVIEW", "+1.0%"), _entry("Buy", "+1.0%", pending=True)]) == {}

def test_a_non_numeric_alpha_is_excluded_not_fatal():
    assert alpha_of(_entry("Buy", "n/a")) is None
    assert score_entries([_entry("Buy", "n/a")]) == {}

def test_direction_covers_all_five_tiers():
    assert DIRECTION == {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alpha_desk_research.memory.scoring'`

- [ ] **Step 3: Create `memory/scoring.py`**

Move `_alpha`, `_DIRECTION` and `RatingScore` verbatim out of `backtest.py`, renaming `_alpha`→`alpha_of` and `_DIRECTION`→`DIRECTION`, and add `score_entries` holding exactly the loop `summarize()` uses today: keep entries that are not pending, whose rating is not `RATING_REVIEW`, and whose alpha parses; group by `key(entry)` or, by default, by `entry["rating"]`; per group `count=len(alphas)`, `hit_rate = sum(a * DIRECTION.get(rating, 0) > 0) / n if direction else None`, `mean_alpha = sum(alphas) / n`.

- [ ] **Step 4: Run the new tests, then the existing ones**

Run: `python3 -m pytest tests/test_scoring.py tests/test_backtest.py tests/test_rating_integrity.py -v`
Expected: all PASS. `tests/test_backtest.py` must pass **unmodified**.

- [ ] **Step 5: Point `backtest.py` at the shared rule**

Delete the moved definitions; import `DIRECTION`, `RatingScore`, `score_entries`, `alpha_of` from `alpha_desk_research.memory.scoring`. `summarize()` keeps its signature, its `BacktestSummary` and its `render()` output; its per-rating loop is replaced by `score_entries(resolved_entries)` and its `unscored`/`pending` counting stays as it is.

- [ ] **Step 6: Verify nothing moved behaviourally**

Run: `python3 -m pytest tests/test_backtest.py tests/test_cli_commands.py -v && ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add alpha_desk_research/memory/scoring.py alpha_desk_research/backtest.py tests/test_scoring.py
git commit -m "Move the rating-scoring rule into memory/scoring.py

backtest.py keeps the grid, the result types and the rendering; the rule that
decides whether a settled decision was right now lives with the log it reads, so
the scoreboard can reuse it instead of forking it. tests/test_backtest.py is
unchanged and green."
```

---

### Task 2: The run index

**Files:**
- Create: `alpha_desk_research/memory/run_index.py`
- Test: `tests/test_run_index.py` (new)

**Interfaces:**
- Produces: `RunRecord(ts, ticker, trade_date, rating, asset_type="stock", signature="", settings={})`; `default_index_path(config: dict) -> Path`; `RunIndex(config: dict | None = None)` with `.path: Path` and `.append(record: RunRecord) -> bool`, `.load() -> tuple[list[RunRecord], int]` (records, malformed-line count)
- Consumes: `alpha_desk_research.dataflows.files.locked` (existing lock helper)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_index.py
from alpha_desk_research.memory.run_index import RunIndex, RunRecord, default_index_path

def _cfg(path):
    return {"memory_log_path": str(path / "trading_memory.md"),
            "run_index_path": str(path / "runs.jsonl")}

def test_appending_records_round_trips(tmp_path):
    idx = RunIndex(_cfg(tmp_path))
    rec = RunRecord(ts="2026-10-10T21:11:51+00:00", ticker="CAT", trade_date="2026-10-09",
                    rating="Buy", settings={"deep_think_llm": "m"})
    assert idx.append(rec) is True
    assert idx.load() == ([rec], 0)

def test_the_index_sits_beside_the_memory_log_when_unset(tmp_path):
    log = {"memory_log_path": str(tmp_path / "trading_memory.md")}
    assert default_index_path(log) == tmp_path / "runs.jsonl"

def test_a_malformed_line_is_skipped_and_counted(tmp_path):
    idx = RunIndex(_cfg(tmp_path))
    idx.append(RunRecord(ts="t", ticker="CAT", trade_date="2026-10-09", rating="Buy"))
    idx.path.write_text(idx.path.read_text() + "{not json\n", encoding="utf-8")
    records, malformed = idx.load()
    assert [r.ticker for r in records] == ["CAT"] and malformed == 1

def test_a_missing_index_is_empty_not_an_error(tmp_path):
    assert RunIndex(_cfg(tmp_path)).load() == ([], 0)

def test_a_write_failure_is_reported_not_raised(tmp_path):
    idx = RunIndex(_cfg(tmp_path))
    idx.path.mkdir()  # a directory where the file belongs: the write must not raise
    assert idx.append(RunRecord(ts="t", ticker="CAT", trade_date="2026-10-09", rating="Buy")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_run_index.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement `memory/run_index.py`**

`RunRecord` is a dataclass with the fields above. `default_index_path(config)` returns `Path(config["memory_log_path"]).with_name("runs.jsonl")`, honouring a `run_index_path` config value when set. `append` serializes with `dataclasses.asdict` and `json.dumps(..., ensure_ascii=False)`, writes one line under `locked(self.path)`, creates the parent directory, and returns `False` with a warning on any `OSError`. `load` reads line by line, `json.loads` each, skips and counts lines that fail to parse or that lack `ticker`/`trade_date`, and returns records in file order.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_run_index.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alpha_desk_research/memory/run_index.py tests/test_run_index.py
git commit -m "Add the run index sidecar

One JSONL line per run attempt beside the memory log, recording the settings
that produced the run. Append-only, tolerant of malformed lines, and never fatal
to the run it describes."
```

---

### Task 3: The `run_index_path` setting

**Files:**
- Modify: `alpha_desk_research/default_config.py` (env map at `:33`; the `DEFAULT_CONFIG` literal near `memory_log_path` at `:86`)
- Test: `tests/test_run_index.py` (add one test)

**Interfaces:**
- Consumes: `default_index_path` (Task 2)
- Produces: config key `run_index_path`, env var `ALPHA_DESK_RUN_INDEX_PATH`

- [ ] **Step 1: Write the failing test**

```python
def test_the_index_path_comes_from_the_environment_like_its_siblings(tmp_path, monkeypatch):
    from alpha_desk_research.default_config import DEFAULT_CONFIG, build_default_config
    monkeypatch.setenv("ALPHA_DESK_RUN_INDEX_PATH", str(tmp_path / "custom.jsonl"))
    assert DEFAULT_CONFIG["run_index_path"].endswith("runs.jsonl")   # import-time default
    assert build_default_config()["run_index_path"] == str(tmp_path / "custom.jsonl")
    assert default_index_path(build_default_config()) == tmp_path / "custom.jsonl"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_run_index.py::test_the_index_path_comes_from_the_environment_like_its_siblings -v`
Expected: FAIL — `KeyError: 'run_index_path'`

- [ ] **Step 3: Add the key, following the sibling path settings**

Add to `build_default_config()`'s literal, beside `memory_log_path` (`default_config.py:86`), the same `os.getenv(...) or os.path.join(_ALPHA_DESK_HOME, ...)` pattern those three path keys use — `"run_index_path": os.getenv("ALPHA_DESK_RUN_INDEX_PATH") or os.path.join(_ALPHA_DESK_HOME, "memory", "runs.jsonl")`. `_ENV_OVERRIDES` is for scalar settings that need type coercion; paths are read directly at build time, so do not add an entry there. `default_index_path` returns `Path(config["run_index_path"]).expanduser()` when set, and falls back to `Path(config["memory_log_path"]).with_name("runs.jsonl")` when it is `None` or empty.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_run_index.py tests/test_dataflows_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alpha_desk_research/default_config.py alpha_desk_research/memory/run_index.py tests/test_run_index.py
git commit -m "Make the run-index path configurable via ALPHA_DESK_RUN_INDEX_PATH"
```

---

### Task 4: Record every attempt from `record_decision`

**Files:**
- Modify: `alpha_desk_research/graph/trading_graph.py` (`record_decision` at `:358-370`, its call at `:399`, and `__init__` where `self.memory_log` is built)
- Modify: `cli/run.py:382`
- Test: `tests/test_run_index.py` (add three tests)

**Interfaces:**
- Consumes: `RunIndex`, `RunRecord` (Task 2); `run_settings()`, `_run_signature()` (existing)
- Produces: `record_decision(company_name, trade_date, final_state, asset_type="stock", portfolio=None)`; `AlphaDeskGraph.run_index`

- [ ] **Step 1: Write the failing test**

```python
def test_a_run_with_no_decision_is_still_counted(tmp_path):
    from alpha_desk_research.graph.trading_graph import AlphaDeskGraph
    graph = AlphaDeskGraph(config={**MINIMAL_CFG, "memory_log_path": str(tmp_path / "log.md")})
    graph.record_decision("NVDA", "2026-01-10", {})
    records, _ = graph.run_index.load()
    assert [r.rating for r in records] == ["REVIEW"]          # nothing tradeable, still a wasted run

def test_a_scored_run_records_its_settings_and_rating(tmp_path):
    graph = _graph(tmp_path)                                   # same helper style as test_cli_memory_log.py
    graph.record_decision("NVDA", "2026-01-10", {"final_trade_decision": "Rating: Buy\n\nbuy"})
    record = graph.run_index.load()[0][0]
    assert record.rating == "Buy" and record.settings["deep_think_llm"] == graph.config["deep_think_llm"]

def test_the_index_file_records_settings_without_paths_or_secrets(tmp_path):
    graph = _graph(tmp_path)
    graph.record_decision("NVDA", "2026-01-10", {"final_trade_decision": "Rating: Buy\n\nbuy"})
    line = json.loads(graph.run_index.path.read_text(encoding="utf-8").splitlines()[0])
    assert not [k for k in line["settings"] if k.endswith(("_path", "_key", "_url"))]
    assert str(tmp_path) not in json.dumps(line["settings"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_run_index.py -v -k record`
Expected: FAIL — `AttributeError: 'AlphaDeskGraph' object has no attribute 'run_index'`

- [ ] **Step 3: Implement the hook**

Build `self.run_index = RunIndex(self.config)` where `self.memory_log` is built. In `record_decision`, before the `if not decision: return` early exit, append `RunRecord(ts=datetime.now(timezone.utc).isoformat(timespec="seconds"), ticker=company_name, trade_date=str(trade_date), rating=run_rating(final_state) if final_state.get("final_trade_decision") else RATING_REVIEW, asset_type=asset_type, signature=self._run_signature(asset_type, portfolio), settings=self.run_settings())`. The append's `False` return is a warning, never an exception. Thread `asset_type`/`portfolio` through the `_run_graph` call site (`:399`) and the CLI call (`cli/run.py:382` passes `selections["asset_type"]` and `portfolio`); `mcp_server/worker.py:181` keeps the defaults, since jobs carry no asset type.

- [ ] **Step 4: Run the new tests and the existing decision tests**

Run: `python3 -m pytest tests/test_run_index.py tests/test_cli_memory_log.py tests/test_graph_end_to_end.py tests/test_mcp_contract.py -v`
Expected: all PASS (the existing 3-arg calls still work through the defaults)

- [ ] **Step 5: Commit**

```bash
git add alpha_desk_research/graph/trading_graph.py cli/run.py tests/test_run_index.py
git commit -m "Record every run attempt in the run index

Written before record_decision's early return, so a run that ends with no
decision is counted as the wasted run it is. Index failures warn, never raise."
```

---

### Task 5: Scoreboard aggregation

**Files:**
- Create: `alpha_desk_research/scoreboard.py`
- Test: `tests/test_scoreboard.py` (new)

**Interfaces:**
- Consumes: `score_entries`, `RatingScore` (Task 1); `RunRecord` (Task 2); `TradingMemoryLog.load_entries()` (existing)
- Produces: `DIMENSIONS`; `GroupScore(label, count, resolved, pending, unscored, hit_rate, mean_alpha, first, last)`; `Coverage(runs_without_entry, entries_without_record, malformed_lines, index_present, repeats)`; `Scoreboard(groups: dict[str, list[GroupScore]], overall: GroupScore, coverage: Coverage, window: str)`; `build_scoreboard(entries, records, *, malformed=0, index_present=True, ticker=None, since=None, until=None, group_by=("rating",)) -> Scoreboard`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scoreboard.py  (helpers: _entries(...) log dicts, _records(...) RunRecords)
def test_the_rating_rows_match_backtest_summarize(tmp_path):
    """Parity: the proof the rule was not forked."""
    path = _seeded_log(tmp_path)                       # same fixture idea as test_backtest.py's _log_with
    board = build_scoreboard(TradingMemoryLog({"memory_log_path": str(path)}).load_entries(), [])
    summary = summarize(path)
    for rating, row in summary.by_rating.items():
        group = _row(board, rating)
        assert (group.resolved, group.hit_rate) == (row.count, row.hit_rate)
        assert round(group.mean_alpha, 10) == round(row.mean_alpha, 10)
    assert board.overall.resolved == summary.resolved and board.overall.unscored == summary.unscored

def test_an_unknown_rating_is_a_row_with_no_hit_rate():          # Review Focus 1
    board = build_scoreboard([_entry("Strong Buy", "+3.0%")], [])
    assert _row(board, "Strong Buy").hit_rate is None and _row(board, "Strong Buy").resolved == 1

def test_review_runs_are_counted_unscored_never_averaged():
    board = build_scoreboard([_entry("REVIEW", None)], [_record(rating="REVIEW")])
    assert board.overall.unscored == 1 and board.overall.mean_alpha is None

def test_a_run_with_no_log_entry_is_counted_as_wasted():          # Review Focus 4
    board = build_scoreboard([], [_record()])
    assert board.coverage.runs_without_entry == 1

def test_a_repeated_run_is_counted_once_last_wins():              # Review Focus 3
    board = build_scoreboard([_entry("Buy", "+1.0%")], [_record(rating="Buy", model="m1"), _record(rating="Buy", model="m2")])
    assert board.coverage.repeats == 1 and _row(board, "m2", "model").count == 1

def test_entries_without_a_record_group_as_unknown():             # Review Focus 2, second half
    board = build_scoreboard([_entry("Buy", "+1.0%")], [])
    assert _row(board, "unknown (no run record)", "model").label == "unknown (no run record)"

def test_a_record_without_settings_groups_as_unknown_not_dropped():
    board = build_scoreboard([_entry("Buy", "+1.0%")], [_record(rating="Buy", settings={})])
    assert _row(board, "unknown (no settings)", "model").resolved == 1

def test_since_and_until_filter_on_trade_date():
    board = build_scoreboard([_entry("Buy", "+1.0%", date="2026-01-05"), _entry("Buy", "+1.0%", date="2026-03-05")], [],
                             since="2026-02-01")
    assert board.overall.count == 1

def test_an_empty_input_is_an_empty_scoreboard():
    assert build_scoreboard([], []).overall.count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scoreboard.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement `scoreboard.py`**

`DIMENSIONS = ("rating", "model", "provider", "analysts", "rounds", "ticker", "period", "horizon")`. Records are keyed `(trade_date, ticker)` with the last line winning and earlier duplicates counted in `coverage.repeats`. Each `GroupScore` counts all filtered entries in the group (`count`), the scored subset (`resolved`, from `score_entries` rows), pending entries, and `REVIEW` entries (`unscored`), and takes `hit_rate`/`mean_alpha` from the same `score_entries` call, so the rule is never re-implemented. Dimension keys: `model` = `settings["deep_think_llm"]`, `provider` = `settings["llm_provider"]`, `analysts` = `",".join(settings["analysts"])`, `rounds` = `f"debate={max_debate_rounds},risk={max_risk_discuss_rounds}"`, `period` = `trade_date[:7]`, `horizon` = the entry's `holding`, `ticker` = the entry's ticker. Missing or non-dict `settings` on a record yields the label `unknown (no settings)`; entries with no record yield `unknown (no run record)`. `window` comes from the resolved entries' `holding` values the way `summarize()` builds it. `overall` is the unfiltered equivalent of `groups["rating"]` summed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scoreboard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alpha_desk_research/scoreboard.py tests/test_scoreboard.py
git commit -m "Add scoreboard aggregation over the memory log and run index

Joins settled outcomes to the settings that produced them, groups by rating,
model, provider, analysts, rounds, ticker, period and horizon, and reports what
it could not attribute instead of guessing. The rating rows are pinned to
backtest.summarize by a parity test."
```

---

### Task 6: Scoreboard rendering

**Files:**
- Modify: `alpha_desk_research/scoreboard.py`
- Test: `tests/test_scoreboard.py` (add tests)

**Interfaces:**
- Consumes: `Scoreboard`, `GroupScore` (Task 5)
- Produces: `render(scoreboard: Scoreboard, *, group_by: tuple[str, ...] = ("rating",), min_sample: int = 10) -> str`; `as_dict(scoreboard: Scoreboard) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_rate_below_the_sample_floor_is_not_printed_as_a_rate():
    text = render(build_scoreboard([_entry("Buy", "+1.0%")] * 3, []), group_by=("rating",))
    assert "too few to rate" in text and "100%" not in text

def test_hold_says_no_direction_claimed_however_many_there_are():
    text = render(build_scoreboard([_entry("Hold", "+1.0%")] * 25, []), group_by=("rating",))
    assert "no direction claimed" in text

def test_the_output_names_the_sample_and_the_independence_caveat():
    text = render(build_scoreboard([_entry("Buy", "+1.0%")], []), group_by=("rating",))
    assert "2026-01-05" in text and "not independent" in text

def test_coverage_is_reported_when_things_do_not_join():
    text = render(build_scoreboard([], [_record()]), group_by=("rating",))
    assert "no log entry" in text

def test_as_dict_round_trips_the_numbers():
    assert as_dict(build_scoreboard([_entry("Buy", "+4.0%")], []))["overall"]["resolved"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scoreboard.py -v -k "rate or direction or sample or coverage or as_dict"`
Expected: FAIL — `NameError: render`

- [ ] **Step 3: Implement `render` and `as_dict`**

Plain text, one line per row: `label: n=…, resolved=…, hit rate 42% (or "too few to rate" / "no direction claimed"), mean alpha +1.2%, window, first…last`. Rows are ordered by descending `count`. `overall` prints first, then each requested dimension. Coverage lines print only when non-zero (or when the index is absent, which is stated once). `as_dict` returns `{"groups": {...}, "overall": {...}, "coverage": {...}, "window": ...}` with the same field names as the dataclasses, for `--json` and the MCP tool.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scoreboard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alpha_desk_research/scoreboard.py tests/test_scoreboard.py
git commit -m "Render the scoreboard, refusing to print a rate the sample cannot support"
```

---

### Task 7: The `scoreboard` CLI command

**Files:**
- Modify: `cli/main.py` (add a command alongside `backtest` at `:107`)
- Test: `tests/test_cli_commands.py` (add tests, following its `CliRunner` + `calls` fixture style)

**Interfaces:**
- Consumes: `build_scoreboard`, `render`, `as_dict` (Tasks 5–6); `RunIndex` (Task 2); `TradingMemoryLog` (existing)
- Produces: `alpha-desk scoreboard [--ticker] [--since] [--until] [--group-by] [--min-sample] [--log] [--json]`

- [ ] **Step 1: Write the failing tests**

```python
def test_scoreboard_prints_the_track_record(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DESK_MEMORY_LOG_PATH", str(_seeded_log(tmp_path)))
    result = runner.invoke(m.app, ["scoreboard"])
    assert result.exit_code == 0 and "overall" in result.stdout

def test_scoreboard_json_is_machine_readable(runner, tmp_path, monkeypatch):
    result = runner.invoke(m.app, ["scoreboard", "--json"])
    assert json.loads(result.stdout)["overall"]["count"] >= 0

def test_scoreboard_can_score_an_arbitrary_log(runner, tmp_path):
    assert runner.invoke(m.app, ["scoreboard", "--log", str(_seeded_log(tmp_path))]).exit_code == 0

def test_scoreboard_does_not_also_run_an_analysis(runner, tmp_path):
    runner.invoke(m.app, ["scoreboard"])
    assert calls == []          # same guard test_cli_commands.py already uses for `backtest`
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_cli_commands.py -v -k scoreboard`
Expected: FAIL — no such command (`exit_code == 2`)

- [ ] **Step 3: Implement the command**

Mirror the `backtest` command's shape: `@app.command()` with `typer.Option`s for the flags, `--group-by` as `list[str]` defaulting to `["rating"]`, `--min-sample` default 10. Resolve the log at invocation time with `build_default_config()` rather than the import-time `DEFAULT_CONFIG`, so `ALPHA_DESK_MEMORY_LOG_PATH` set for this process is honoured (that is how the tests point at a seeded log, and it is what `backtest` needs too); `--log` overrides the path outright, and the index is read from `runs.jsonl` beside whichever log won. Print `render(...)` via `cli.display.console`, or under `--json` write `json.dumps(as_dict(...), indent=2)` straight to `sys.stdout` — not through `console`, which soft-wraps at the terminal width and would hand back unparseable text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_cli_commands.py -v`
Expected: PASS

- [ ] **Step 5: Smoke it for real**

Run: `python3 -m cli.main scoreboard --json | head -20` in the repo (the live memory log may be empty of resolved entries — that is a valid empty scoreboard, not a crash)
Expected: valid JSON with `overall`, `groups`, `coverage`, `window`.

- [ ] **Step 6: Commit**

```bash
git add cli/main.py tests/test_cli_commands.py
git commit -m "Add the scoreboard command

Reads the live memory log and its run index, or any log given with --log, which
is how two configurations get compared."
```

---

### Task 8: Report card in the HTML report

**Files:**
- Modify: `alpha_desk_research/reporting.py:92-93` (`write_report_tree`)
- Modify: `alpha_desk_research/report_html.py:248` (`render_report`)
- Modify: `alpha_desk_research/graph/trading_graph.py:277-286` (`save_reports`)
- Test: `tests/test_reporting.py`, `tests/test_report_html.py`

**Interfaces:**
- Consumes: `build_scoreboard`, `render` (Tasks 5–6)
- Produces: `write_report_tree(final_state, ticker, save_path, settings=None, html=True, scoreboard=None) -> Path`; `render_report(ticker, final_state, settings, parts, scoreboard=None) -> str`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_report_without_a_scoreboard_is_unchanged(tmp_path):
    assert write_report_tree(_state(), "NVDA", tmp_path).read_text() == \
        write_report_tree(_state(), "NVDA", tmp_path / "b").read_text()

def test_the_page_gains_a_track_record_section_when_data_exists(tmp_path):
    board = build_scoreboard([("NVDA", "Buy", "+4.0%")], [])
    page = (tmp_path / "complete_report.html")
    write_report_tree(_state(), "NVDA", tmp_path, html=True, scoreboard=board)
    assert "Track record" in page.read_text() and "mean alpha" in page.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_report_html.py -v -k track_record`
Expected: FAIL — `TypeError: write_report_tree() got an unexpected keyword argument 'scoreboard'`

- [ ] **Step 3: Thread the optional section through**

`scoreboard=None` must leave the markdown tree and the page exactly as they are today (the first test is the guard). When a `Scoreboard` is passed and `html=True`, `render_report` adds a `Track record` section after the contents: the `overall` row plus the rows for this ticker, using `render(...)`. In `save_reports`, build it from the graph's own log and index and pass it only when building HTML:

```python
records, malformed = self.run_index.load()          # malformed is keyword-only on build_scoreboard
board = build_scoreboard(self.memory_log.load_entries(), records,
                         malformed=malformed, group_by=("rating",))
```

A failure to build it must not lose the report: catch it, warn, pass `None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_reporting.py tests/test_report_html.py tests/test_cli_memory_log.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alpha_desk_research/reporting.py alpha_desk_research/report_html.py alpha_desk_research/graph/trading_graph.py tests/test_reporting.py tests/test_report_html.py
git commit -m "Put the track record in the saved report

save_reports builds it from the run's own log and index, so every CLI run and
every MCP job picks it up without a caller change. No data, no section, and the
page is byte-identical to before."
```

---

### Task 9: MCP `get_scoreboard`

**Files:**
- Modify: `mcp_server/server.py` (module-level function near `get_decision` at `:190`, `tool_*` wrapper near `:251`, `build_app` at `:258-300`)
- Test: `tests/test_mcp_contract.py` (add tests; its `REPORT_KEYS`-style constant pattern applies)

**Interfaces:**
- Consumes: `build_scoreboard`, `as_dict` (Tasks 5–6)
- Produces: `get_scoreboard(ticker: str | None = None, since: str | None = None, group_by: str | None = None) -> dict`; `tool_get_scoreboard(...) -> str`; the `get_scoreboard` MCP tool

- [ ] **Step 1: Write the failing tests**

```python
def test_the_mcp_tool_agrees_with_the_cli_json(tmp_path, monkeypatch):
    """Acceptance criterion 4: same log, same numbers, whoever asks."""
    monkeypatch.setenv("ALPHA_DESK_MEMORY_LOG_PATH", str(_seeded_log(tmp_path)))
    from cli.main import app
    from typer.testing import CliRunner
    cli_out = json.loads(CliRunner().invoke(app, ["scoreboard", "--json"]).stdout)
    assert json.loads(tool_get_scoreboard())["overall"] == cli_out["overall"]

def test_the_mcp_tool_does_not_raise_on_an_empty_log(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_DESK_MEMORY_LOG_PATH", str(tmp_path / "none.md"))
    assert json.loads(tool_get_scoreboard())["overall"]["count"] == 0

def test_an_invalid_dates_or_group_returns_an_error_string_not_a_traceback():
    assert tool_get_scoreboard(since="not-a-date").startswith("ERROR:")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp_contract.py -v -k scoreboard`
Expected: FAIL — cannot import `get_scoreboard`

- [ ] **Step 3: Implement the tool**

Follow the existing pattern exactly: module-level `get_scoreboard` returning `as_dict(...)`, a `tool_get_scoreboard` that wraps it in `json.dumps` and returns `_err(exc)` on failure (like `tool_get_decision`), and an `@app.tool(name="get_scoreboard")` wrapper in `build_app` whose docstring says it reads the settled track record. Update `build_app`'s "six research tools" docstring to seven.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server/server.py tests/test_mcp_contract.py
git commit -m "Expose the scoreboard over MCP

Read-only, same numbers as the CLI, so an agent can ask whether the desk's calls
have been worth acting on before it acts on the next one."
```

---

### Task 10: Per-config cost matrix (separable slice)

**Files:**
- Modify: `benchmarks/run_baseline.py` (`run` at `:72`, `main` at `:141`)
- Create: `benchmarks/run_matrix.py`
- Test: `tests/test_benchmark_matrix.py` (new; only the pure merge and aggregation are tested — no LLM, no network)

**Interfaces:**
- Consumes: `run_baseline.run`; `DEFAULT_CONFIG` (existing)
- Produces: `merged_config(overrides: dict | None) -> dict` in `benchmarks/run_baseline.py`; `run(ticker, trade_date, overrides=None) -> dict`; `benchmarks/run_matrix.py:main(argv)` writing `benchmarks/results/matrix_<stamp>/{matrix.json,matrix.md}`

- [ ] **Step 1: Write the failing tests**

```python
def test_overrides_are_merged_over_the_defaults():
    cfg = merged_config({"deep_think_llm": "cheap", "max_debate_rounds": 1})
    assert cfg["deep_think_llm"] == "cheap" and cfg["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]

def test_the_matrix_summarizes_cost_per_config():
    table = summarize_matrix([{"config": "a", "tokens": {"total": {"total_tokens": 100}}, "wall_total_s": 9.0, "ok": True},
                              {"config": "a", "tokens": {"total": {"total_tokens": 300}}, "wall_total_s": 11.0, "ok": False}])
    assert table[0]["config"] == "a" and table[0]["runs"] == 2 and table[0]["ok"] == 1 and table[0]["median_tokens"] == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_benchmark_matrix.py -v`
Expected: FAIL — no module / no `merged_config`

- [ ] **Step 3: Implement the merge, the matrix runner and its aggregation**

`merged_config` returns `{**DEFAULT_CONFIG, **(overrides or {})}`. `run` accepts `overrides`, uses `merged_config` for its config, and keeps its existing envelope (adding a `config_overrides` key is fine — nothing pins that JSON). `run_matrix.py` takes `--tickers`, `--trade-date` (repeatable or a grid of dates), `--configs` (a JSON file of named override objects), runs one baseline per config × date, and writes `matrix.json` plus a markdown table via `summarize_matrix` (runs, ok count, median wall seconds, median total tokens per config).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_benchmark_matrix.py -v && ruff check .`
Expected: PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add benchmarks/run_baseline.py benchmarks/run_matrix.py tests/test_benchmark_matrix.py
git commit -m "Add a per-config cost matrix to the benchmarks

Config overrides for the baseline runner and a matrix that reports wall time,
tokens and ok-rate per configuration. Quality per config comes from the
scoreboard pointed at that config's backtest log."
```

---

## Execution notes

- Task order is the dependency order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8/9 → 10. Tasks 7, 8 and 9 are independent of each other once 6 lands; 10 depends on nothing but is last so the scoreboard exists to judge its output.
- Tasks 1 and 4 touch code the whole suite exercises: run the full suite (`python3 -m pytest -q`) after each, not just the listed files.
- Task 4 changes a public signature (`record_decision`) with defaults, so existing 3-argument callers keep working; the CLI pass-through is the only one that gains information.
- Wiring the report card (Task 8) through `save_reports` means the MCP worker picks it up too, but it writes `html=False`, so MCP jobs gain no page — intentional, and the markdown tree is unchanged either way.
