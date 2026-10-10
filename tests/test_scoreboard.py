"""The scoreboard: the log's settled outcomes, joined to the runs that produced them.

The scoring rule is not forked here. The rating rows are pinned to
``backtest.summarize`` field for field, and every other grouping is scored by the
same ``score_entries`` call, so a row can only be as wrong as the rule the
backtest already uses.
"""

from __future__ import annotations

import pytest

from alpha_desk_research.backtest import summarize
from alpha_desk_research.memory import TradingMemoryLog
from alpha_desk_research.memory.run_index import RunRecord
from alpha_desk_research.scoreboard import DIMENSIONS, build_scoreboard


def _entry(rating, alpha, *, date="2026-01-05", ticker="NVDA", holding="5d", pending=False):
    """A memory-log entry, shaped the way ``load_entries()`` returns one."""
    return {
        "date": date,
        "ticker": ticker,
        "rating": rating,
        "pending": pending,
        "raw": None,
        "alpha": alpha,
        "holding": holding,
        "resolved": None,
        "decision": "",
        "reflection": "",
    }


def _record(
    *,
    ticker="NVDA",
    date="2026-01-05",
    rating="Buy",
    settings=None,
    model="deep",
    provider="openai",
    analysts=("market", "news"),
    rounds=(1, 1),
):
    """A run record carrying the ``run_settings()`` allowlist, unless overridden."""
    if settings is None:
        settings = {
            "deep_think_llm": model,
            "llm_provider": provider,
            "analysts": list(analysts),
            "max_debate_rounds": rounds[0],
            "max_risk_discuss_rounds": rounds[1],
        }
    return RunRecord(
        ts="2026-01-05T09:30:00+00:00",
        ticker=ticker,
        trade_date=date,
        rating=rating,
        settings=settings,
    )


def _row(board, label, dimension="rating"):
    for row in board.groups.get(dimension, []):
        if row.label == label:
            return row
    raise AssertionError(f"no {label!r} row under {dimension!r}: {board.groups.get(dimension)}")


def _seeded_log(tmp_path):
    """A log with two wins, a loss, a Hold, a pending cell and an unreadable REVIEW."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    rows = [
        ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04)),
        ("NVDA", "2026-01-12", "Rating: Buy\n\nx", (-0.02, -0.02)),
        ("AAPL", "2026-01-05", "Rating: Sell\n\nx", (0.05, 0.03)),
        ("MSFT", "2026-01-05", "Rating: Hold\n\nx", (0.01, 0.005)),
        ("AMD", "2026-01-05", "no readable rating", (0.03, 0.02)),
        ("TSLA", "2026-01-05", "Rating: Buy\n\nx", None),
    ]
    for ticker, date, decision, outcome in rows:
        log.store_decision(ticker, date, decision, rating="REVIEW" if ticker == "AMD" else None)
        if outcome is not None:
            log.update_with_outcome(ticker, date, outcome[0], outcome[1], 5, "note", "2026-02-01")
    return tmp_path / "m.md"


@pytest.mark.unit
def test_the_rating_rows_match_backtest_summarize(tmp_path):
    """Parity: the proof the rule was not forked."""
    path = _seeded_log(tmp_path)  # same fixture idea as test_backtest.py's _log_with
    board = build_scoreboard(TradingMemoryLog({"memory_log_path": str(path)}).load_entries(), [])
    summary = summarize(path)
    for rating, row in summary.by_rating.items():
        group = _row(board, rating)
        assert (group.resolved, group.hit_rate) == (row.count, row.hit_rate)
        assert round(group.mean_alpha, 10) == round(row.mean_alpha, 10)
    assert board.overall.resolved == summary.resolved and board.overall.unscored == summary.unscored


@pytest.mark.unit
def test_an_unknown_rating_is_a_row_with_no_hit_rate():  # Review Focus 1
    board = build_scoreboard([_entry("Strong Buy", "+3.0%")], [])
    assert _row(board, "Strong Buy").hit_rate is None and _row(board, "Strong Buy").resolved == 1


@pytest.mark.unit
def test_review_runs_are_counted_unscored_never_averaged():
    board = build_scoreboard([_entry("REVIEW", None)], [_record(rating="REVIEW")])
    assert board.overall.unscored == 1 and board.overall.mean_alpha is None


@pytest.mark.unit
def test_a_run_with_no_log_entry_is_counted_as_wasted():  # Review Focus 4
    board = build_scoreboard([], [_record()])
    assert board.coverage.runs_without_entry == 1


@pytest.mark.unit
def test_a_repeated_run_is_counted_once_last_wins():  # Review Focus 3
    board = build_scoreboard(
        [_entry("Buy", "+1.0%")],
        [_record(rating="Buy", model="m1"), _record(rating="Buy", model="m2")],
    )
    assert board.coverage.repeats == 1 and _row(board, "m2", "model").count == 1


@pytest.mark.unit
def test_entries_without_a_record_group_as_unknown():  # Review Focus 2, second half
    board = build_scoreboard([_entry("Buy", "+1.0%")], [])
    assert _row(board, "unknown (no run record)", "model").label == "unknown (no run record)"


@pytest.mark.unit
def test_a_record_without_settings_groups_as_unknown_not_dropped():
    board = build_scoreboard([_entry("Buy", "+1.0%")], [_record(rating="Buy", settings={})])
    assert _row(board, "unknown (no settings)", "model").resolved == 1


@pytest.mark.unit
def test_since_and_until_filter_on_trade_date():
    board = build_scoreboard(
        [_entry("Buy", "+1.0%", date="2026-01-05"), _entry("Buy", "+1.0%", date="2026-03-05")],
        [],
        since="2026-02-01",
    )
    assert board.overall.count == 1


@pytest.mark.unit
def test_an_empty_input_is_an_empty_scoreboard():
    assert build_scoreboard([], []).overall.count == 0


# --- the rest of the contract the surfaces read -------------------------------


@pytest.mark.unit
def test_the_overall_row_is_the_ungrouped_whole_and_is_labelled():
    board = build_scoreboard([_entry("Buy", "+1.0%"), _entry("Sell", "-3.0%", ticker="AMD")], [])
    assert board.overall.label == "overall"
    assert (board.overall.count, board.overall.resolved) == (2, 2)
    assert board.overall.hit_rate == 1.0


@pytest.mark.unit
def test_the_board_carries_every_dimension_and_leads_with_the_requested_one():
    board = build_scoreboard([_entry("Buy", "+1.0%")], [], group_by=("model", "rating"))
    assert set(board.groups) == set(DIMENSIONS)
    assert list(board.groups)[:2] == ["model", "rating"]


@pytest.mark.unit
@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_every_dimension_partitions_the_entries(dimension):
    """A row per dimension's value, and nothing dropped between the rows."""
    entries = [
        _entry("Buy", "+1.0%", ticker="NVDA", date="2026-01-05"),
        _entry("Sell", "-2.0%", ticker="AMD", date="2026-02-05", holding="21d"),
    ]
    records = [
        _record(ticker="NVDA", date="2026-01-05"),
        _record(ticker="AMD", date="2026-02-05", rating="Sell", rounds=(2, 3)),
    ]
    board = build_scoreboard(entries, records, group_by=(dimension,))
    assert sum(row.count for row in board.groups[dimension]) == 2
    assert sum(row.resolved for row in board.groups[dimension]) == 2


@pytest.mark.unit
def test_coverage_reports_what_could_not_be_joined():
    board = build_scoreboard(
        [_entry("Buy", "+1.0%")], [_record(ticker="AMD")], malformed=2, index_present=False
    )
    assert (
        board.coverage.entries_without_record,
        board.coverage.runs_without_entry,
        board.coverage.malformed_lines,
        board.coverage.index_present,
    ) == (1, 1, 2, False)


@pytest.mark.unit
def test_a_row_and_the_scoreboard_carry_the_date_range_and_window_the_sample_used():
    board = build_scoreboard(
        [
            _entry("Buy", "+1.0%", date="2026-01-05"),
            _entry("Buy", "+2.0%", date="2026-03-05", holding="21d"),
        ],
        [],
    )
    row = _row(board, "Buy")
    assert (row.first, row.last) == ("2026-01-05", "2026-03-05")
    assert board.window == "21 trading days, 5 trading days"


@pytest.mark.unit
def test_an_unknown_dimension_or_a_non_iso_date_is_rejected():
    with pytest.raises(ValueError):
        build_scoreboard([], [], group_by=("nonsense",))
    with pytest.raises(ValueError):
        build_scoreboard([], [], since="not-a-date")
    with pytest.raises(ValueError):
        build_scoreboard([], [], until="2026-1-5")
