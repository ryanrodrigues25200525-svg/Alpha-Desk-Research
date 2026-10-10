import json
import sys
from pathlib import Path

import typer

from alpha_desk_research.backtest import iter_grid, run_backtest, summarize
from alpha_desk_research.default_config import DEFAULT_CONFIG, build_default_config
from alpha_desk_research.memory import TradingMemoryLog
from alpha_desk_research.memory.run_index import RunIndex
from alpha_desk_research.portfolio import load_portfolio
from alpha_desk_research.scoreboard import as_dict, build_scoreboard, render
from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="alpha-desk",
    help="Alpha Desk Research CLI: multi-agent LLM equity research desk",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor ALPHA_DESK_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
    no_live: bool = typer.Option(
        False, "--no-live",
        help="Skip the alternate-screen live view; print plain progress lines instead (pipes, CI, MCP harnesses)",
    ),
):
    """Run an analysis. This is what a bare `alpha-desk` does.

    Flags answer their questions; with provider, models, depth and language also
    set through ALPHA_DESK_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from alpha_desk_research.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html, "no_live": no_live}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


# The only flag whose default is a list: ruff's B008 rejects a mutable call in a
# default value, so the option object is built here instead of in the signature.
_GROUP_BY_OPTION = typer.Option(
    ["rating"],
    "--group-by",
    help="Dimension to split the board by: rating, model, provider, analysts, rounds, ticker, "
    "period or horizon; repeat the flag to print more than one",
)


@app.command()
def scoreboard(
    ticker: str = typer.Option(None, "--ticker", help="Score only this ticker's decisions"),
    since: str = typer.Option(None, "--since", help="Earliest trade date to score, YYYY-MM-DD"),
    until: str = typer.Option(None, "--until", help="Latest trade date to score, YYYY-MM-DD"),
    group_by: list[str] = _GROUP_BY_OPTION,
    min_sample: int = typer.Option(
        10, "--min-sample", help="Directional decisions a row needs before its hit rate is printed"
    ),
    log: str = typer.Option(
        None,
        "--log",
        help="Memory log to score instead of the live one, reading the runs.jsonl beside it",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print the board as JSON instead of text"
    ),
):
    """What the desk's settled calls earned, grouped by what produced them."""
    # The log is resolved now rather than from the import-time DEFAULT_CONFIG, so
    # ALPHA_DESK_MEMORY_LOG_PATH set for this process is the log that is read.
    config = build_default_config()
    if log:
        config["memory_log_path"] = str(Path(log).expanduser())
        # --log names a log to score on its own terms, so its index is the
        # runs.jsonl beside it and not the live desk's.
        config.pop("run_index_path", None)
    log_path = Path(config["memory_log_path"])
    if log and not log_path.is_file():
        console.print(
            f"[red]No memory log at {log_path}; --log must name an existing log file[/red]"
        )
        raise typer.Exit(code=1)

    index = RunIndex(config)
    records, malformed = index.load()
    try:
        board = build_scoreboard(
            TradingMemoryLog(config).load_entries(),
            records,
            malformed=malformed,
            index_present=index.path.exists(),
            ticker=ticker,
            since=since,
            until=until,
            group_by=tuple(group_by),
        )
        if json_output:
            output = json.dumps(as_dict(board), indent=2)
        else:
            output = render(board, group_by=tuple(group_by), min_sample=min_sample)
    except ValueError as exc:  # an unknown dimension or a date that is not one
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        # Straight to stdout: rich soft-wraps at the terminal width, which would
        # hand a caller a document that no longer parses.
        typer.echo(output)
    else:
        console.print(output, markup=False)


if __name__ == "__main__":
    app()
