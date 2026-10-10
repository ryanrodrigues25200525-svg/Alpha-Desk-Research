"""Per-config cost matrix: run the baseline once per config x ticker x date.

Wall time and total tokens per configuration. Cost is not a verdict, so the
matrix only carries the numbers; quality per config comes from the scoreboard
pointed at that config's backtest log (the markdown says so).

Usage:
    python benchmarks/run_matrix.py --configs cfgs.json --tickers NVDA --trade-date 2026-01-05
    python benchmarks/run_matrix.py --configs cfgs.json --tickers NVDA,AAPL --trade-date 2026-01-05 --trade-date 2026-01-06

The configs file maps a config name to the overrides merged over DEFAULT_CONFIG
for that config's runs. Without LLM/vendor keys every run fails and is recorded
as ``ok: false`` — the matrix still names each config, since the cost of a
config that fails is worth knowing. Exit code is 0 when at least one run
succeeded, 1 when every run failed, 2 when the arguments are unusable.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

# Invoked by path (python benchmarks/run_matrix.py) sys.path[0] is benchmarks/,
# while as a module or under pytest the repository root is: put the root first
# so the sibling baseline and the package it imports resolve either way.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.run_baseline import (  # noqa: E402
    default_trade_date,
    failure_envelope,
    run as run_baseline,
)

RESULTS_DIR = Path("benchmarks/results")


def _token_total(row: dict) -> int | None:
    """The run envelope's ``tokens.total.total_tokens``, or None if it has none."""
    tokens = row.get("tokens")
    if not isinstance(tokens, dict):
        return None
    total = tokens.get("total")
    if not isinstance(total, dict):
        return None
    return total.get("total_tokens")


def _median(values: list) -> float | int | None:
    """Median of the values a run actually recorded, None when none did."""
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def summarize_matrix(rows: list[dict]) -> list[dict]:
    """One row per config, in first-appearance order: runs, ok count, medians.

    A failed run still contributes its wall time and token total when it
    recorded them; the median then also says what a config that fails costs.
    """
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        name = row["config"]
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(row)

    table = []
    for name in order:
        runs = grouped[name]
        table.append(
            {
                "config": name,
                "runs": len(runs),
                "ok": sum(1 for row in runs if row.get("ok")),
                "median_wall_s": _median([row.get("wall_total_s") for row in runs]),
                "median_tokens": _median([_token_total(row) for row in runs]),
            }
        )
    return table


def _fmt(value) -> str:
    return "-" if value is None else str(round(value, 2))


def render_markdown(summary: list[dict]) -> str:
    """The matrix as a markdown table, with the pointer at the quality half."""
    lines = [
        "# Per-config cost matrix",
        "",
        "Medians over each config's runs; a failed run still counts for what it cost.",
        "",
        "| config | runs | ok | median wall s | median tokens |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {row['config']} | {row['runs']} | {row['ok']} | "
        f"{_fmt(row['median_wall_s'])} | {_fmt(row['median_tokens'])} |"
        for row in summary
    ]
    lines += [
        "",
        "Cost is not a verdict: run the backtest per config, then read that config's own log with"
        " `alpha-desk scoreboard --log <that run's log>` — quality is the scoreboard's job, not this table's.",
        "",
    ]
    return "\n".join(lines)


def load_configs(path: str) -> dict[str, dict]:
    """The named override objects in a --configs JSON file. Raises ValueError."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read --configs file {path}: {exc}") from exc
    try:
        configs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--configs file {path} is not valid JSON: {exc}") from exc
    if not isinstance(configs, dict) or not configs:
        raise ValueError(
            f"--configs file {path} must be a JSON object mapping a config name to its overrides"
        )
    for name, overrides in configs.items():
        if not isinstance(overrides, dict):
            raise ValueError(
                f"--configs entry {name!r} must be an object of overrides, got {type(overrides).__name__}"
            )
    return configs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-config cost matrix: baseline wall time and tokens per configuration."
    )
    parser.add_argument("--tickers", default="AAPL", help="Comma-separated tickers")
    parser.add_argument(
        "--trade-date",
        action="append",
        dest="trade_dates",
        default=None,
        help="Trade date YYYY-MM-DD; repeat it to run every config against every date",
    )
    parser.add_argument(
        "--configs", required=True, help="JSON file mapping a config name to its overrides"
    )
    parser.add_argument("--out-dir", default=None, help="Where to write matrix.json/matrix.md")
    ns = parser.parse_args(argv)

    try:
        configs = load_configs(ns.configs)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    tickers = [ticker.strip() for ticker in ns.tickers.split(",") if ticker.strip()]
    if not tickers:
        print("error: --tickers listed no tickers", file=sys.stderr)
        return 2
    trade_dates = ns.trade_dates or [default_trade_date()]

    rows: list[dict] = []
    for name, overrides in configs.items():
        for ticker in tickers:
            for trade_date in trade_dates:
                print(f"running {name} {ticker} {trade_date} ...", flush=True)
                try:
                    envelope = run_baseline(ticker, trade_date, overrides)
                except Exception as exc:  # keys missing, vendor/LLM down: record, keep going
                    envelope = failure_envelope(ticker, trade_date, exc, overrides)
                rows.append({**envelope, "config": name})

    summary = summarize_matrix(rows)
    out_dir = (
        Path(ns.out_dir) if ns.out_dir else RESULTS_DIR / f"matrix_{datetime.now():%Y%m%d_%H%M%S}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    matrix = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tickers": tickers,
        "trade_dates": trade_dates,
        "configs": configs,
        "summary": summary,
        "runs": rows,
    }
    (out_dir / "matrix.json").write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    (out_dir / "matrix.md").write_text(render_markdown(summary), encoding="utf-8")
    print(f"wrote {out_dir / 'matrix.json'} and {out_dir / 'matrix.md'}")
    return 0 if any(row.get("ok") for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
