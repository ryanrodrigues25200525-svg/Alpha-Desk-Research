"""The CLI keeps running an analysis with no arguments, and gains `backtest`.

Every documented invocation is bare (`alpha_desk_research --checkpoint`), so analysis
has to stay the default action while a second command exists alongside it.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import cli.main as m
from alpha_desk_research.memory import TradingMemoryLog


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(m, "run_analysis", lambda **kw: calls.append(("analysis", kw)))
    calls.clear()
    return CliRunner()


calls: list = []


@pytest.mark.unit
def test_no_arguments_still_runs_an_analysis(runner):
    assert runner.invoke(m.app, []).exit_code == 0
    no_flags = {"ticker": None, "date": None, "analysts": None, "save": None, "show": None, "html": None, "no_live": False}
    assert calls == [("analysis", {"checkpoint": None, "portfolio": None, "flags": no_flags})]


@pytest.mark.unit
def test_options_still_parse_without_a_subcommand(runner):
    assert runner.invoke(m.app, ["--checkpoint"]).exit_code == 0
    assert calls[0][1]["checkpoint"] is True


@pytest.mark.unit
def test_backtest_does_not_also_run_an_analysis(runner, monkeypatch, tmp_path):
    swept = []
    monkeypatch.setattr(m, "run_backtest", lambda *a, **kw: swept.append((a, kw)) or _Result(tmp_path))
    monkeypatch.setattr(m, "summarize", lambda log: _Summary())

    result = runner.invoke(m.app, ["backtest", "NVDA,AAPL", "--start", "2026-06-01",
                                   "--end", "2026-06-15", "--every", "7"])

    assert result.exit_code == 0, result.output
    assert calls == []  # the interactive analysis must not run
    (tickers, dates, _config), kwargs = swept[0]
    assert tickers == ["NVDA", "AAPL"]
    assert dates == ["2026-06-01", "2026-06-08", "2026-06-15"]
    assert "scored" in result.output


@pytest.mark.unit
def test_backtest_reports_a_bad_date_instead_of_a_traceback(runner):
    result = runner.invoke(m.app, ["backtest", "NVDA", "--start", "June", "--end", "2026-06-15"])
    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.output


@pytest.mark.unit
def test_help_lists_the_backtest_command(runner):
    assert "backtest" in runner.invoke(m.app, ["--help"]).output


class _Result:
    def __init__(self, tmp_path):
        self.run_id = "20260916_000000"
        self.log_path = tmp_path / "trading_memory.md"
        self.cells_run = 2
        self.skipped = 0
        self.failures = []
        self.settlement_failures = []


class _Summary:
    def render(self):
        return "scored 2 cells"


@pytest.mark.unit
def test_every_command_is_registered_when_run_as_a_module():
    """README documents `python -m cli.main`, which executes the file top to
    bottom, so a command defined after the __main__ block would not exist."""
    import re
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "cli.main", "backtest", "--help"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    # Where the terminal takes colour, help styles each option and splits
    # "--start" across escape sequences, so read the text without them.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)
    assert "--start" in plain


@pytest.mark.unit
def test_backtest_can_continue_an_interrupted_sweep(runner, monkeypatch, tmp_path):
    """Resuming is what makes a long sweep practical, and the Python API has it."""
    swept = []
    monkeypatch.setattr(m, "run_backtest", lambda *a, **kw: swept.append(kw) or _Result(tmp_path))
    monkeypatch.setattr(m, "summarize", lambda log: _Summary())

    result = runner.invoke(m.app, ["backtest", "NVDA", "--start", "2026-06-01",
                                   "--end", "2026-06-08", "--run-id", "20260617_120000"])

    assert result.exit_code == 0, result.output
    assert swept[0]["run_id"] == "20260617_120000"


@pytest.mark.unit
@pytest.mark.parametrize("args, expected", [
    (["backtest", "NVDA", "--start", "2026-08-01", "--end", "2026-06-08"], "before"),
    (["backtest", ",,", "--start", "2026-06-01", "--end", "2026-06-08"], "ticker"),
])
def test_backtest_rejects_input_that_would_sweep_nothing(runner, args, expected):
    """An inverted range or an empty ticker list reported a clean zero-cell run,
    which reads as 'nothing to find' rather than 'you asked for nothing'."""
    result = runner.invoke(m.app, args)
    assert result.exit_code == 1
    assert expected in result.output.lower()


@pytest.mark.unit
def test_backtest_reports_a_setup_failure_in_one_line(runner, monkeypatch):
    """A missing key or a bad analyst name produced a raw traceback."""
    def _explode(*a, **kw):
        raise ValueError("API key for provider 'openai' is not set")

    monkeypatch.setattr(m, "run_backtest", _explode)
    result = runner.invoke(m.app, ["backtest", "NVDA", "--start", "2026-06-01", "--end", "2026-06-08"])

    assert result.exit_code == 1
    assert "API key" in result.output
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_backtest_shows_each_cell_and_how_to_continue(runner, monkeypatch, tmp_path):
    def sweep(tickers, dates, config, progress=None, **kw):
        for i, date in enumerate(dates, 1):
            progress(i, len(dates), tickers[0], date)
        return _Result(tmp_path)

    monkeypatch.setattr(m, "run_backtest", sweep)
    monkeypatch.setattr(m, "summarize", lambda log: _Summary())

    result = runner.invoke(m.app, ["backtest", "NVDA", "--start", "2026-06-01", "--end", "2026-06-08"])

    assert "[1/2] NVDA 2026-06-01" in result.output and "[2/2] NVDA 2026-06-08" in result.output
    assert _Result(tmp_path).run_id in result.output  # id, not the flag: narrow consoles wrap "--run-id <id>"


@pytest.mark.unit
@pytest.mark.parametrize("args, analysts", [
    pytest.param(["--analysts", "sentiment,market"], ["market", "social"], id="named_as_in_analyze"),
    pytest.param(["--asset-type", "crypto"], ["market", "social", "news"], id="crypto_default"),
])
def test_backtest_takes_its_analysts_as_an_analysis_does(runner, monkeypatch, tmp_path, args, analysts):
    swept = []
    monkeypatch.setattr(m, "run_backtest", lambda *a, **kw: swept.append(kw) or _Result(tmp_path))
    monkeypatch.setattr(m, "summarize", lambda log: _Summary())

    result = runner.invoke(m.app, ["backtest", "NVDA", "--start", "2026-06-01", "--end", "2026-06-01", *args])

    assert result.exit_code == 0, result.output
    assert swept[0]["selected_analysts"] == analysts


@pytest.mark.unit
@pytest.mark.parametrize("args", [
    pytest.param(["--analysts", "fundamentals", "--asset-type", "crypto"], id="unavailable_for_crypto"),
    pytest.param(["--asset-type", "bond"], id="unknown_asset_type"),
])
def test_backtest_refuses_a_selection_before_the_sweep(runner, monkeypatch, args):
    monkeypatch.setattr(m, "run_backtest", lambda *a, **kw: pytest.fail("the sweep started"))

    result = runner.invoke(m.app, ["backtest", "NVDA", "--start", "2026-06-01", "--end", "2026-06-01", *args])

    assert result.exit_code == 1


def _seeded_log(tmp_path):
    """A memory log with settled decisions, written through the log's own writer.

    Two outcomes over a Buy and a Sell put a rate on the rating rows, and one
    decision is left pending so the board says how much of the log is settled.
    """
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    rows = [
        ("NVDA", "2026-01-05", "Rating: Buy\n\nbuy it", (0.10, 0.04)),
        ("AAPL", "2026-01-05", "Rating: Sell\n\nsell it", (0.05, 0.03)),
        ("TSLA", "2026-01-12", "Rating: Buy\n\nbuy it", None),
    ]
    for ticker, date, decision, outcome in rows:
        log.store_decision(ticker, date, decision)
        if outcome is not None:
            log.update_with_outcome(ticker, date, outcome[0], outcome[1], 5, "note", "2026-02-01")
    return tmp_path / "m.md"


@pytest.mark.unit
def test_scoreboard_prints_the_track_record(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DESK_MEMORY_LOG_PATH", str(_seeded_log(tmp_path)))
    result = runner.invoke(m.app, ["scoreboard"])
    assert result.exit_code == 0 and "overall" in result.stdout


@pytest.mark.unit
def test_scoreboard_json_is_machine_readable(runner, tmp_path, monkeypatch):
    result = runner.invoke(m.app, ["scoreboard", "--json"])
    assert json.loads(result.stdout)["overall"]["count"] >= 0


@pytest.mark.unit
def test_scoreboard_can_score_an_arbitrary_log(runner, tmp_path):
    assert runner.invoke(m.app, ["scoreboard", "--log", str(_seeded_log(tmp_path))]).exit_code == 0


@pytest.mark.unit
def test_scoreboard_does_not_also_run_an_analysis(runner, tmp_path):
    runner.invoke(m.app, ["scoreboard"])
    assert calls == []  # same guard test_cli_commands.py already uses for `backtest`
