"""The scoreboard: what the desk's settled calls earned, grouped by what produced them.

The memory log holds the outcome -- a decision, the alpha it earned, the holding
window it was measured over -- but not what produced it, so the log alone cannot
answer "did the model with the longer debate do better?" or "is the desk worse on
small caps?". The run index beside it holds the settings, so this module joins the
two on ``(trade_date, ticker)`` and groups the settled decisions by rating, model,
provider, analysts, rounds, ticker, period or horizon.

Two rules shape it:

- The scoring rule is never re-implemented here. Every row's ``hit_rate`` and
  ``mean_alpha`` come from ``score_entries``, the same call ``backtest.summarize``
  uses, and a parity test pins the rating rows to that summary field for field.
- Attribution never guesses. An entry with no run record, or one whose settings
  cannot say what produced it, is filed under ``unknown (...)`` and counted in
  ``coverage`` rather than dropped or attributed to whatever ran nearby.

It reads; it writes nothing. ``render`` turns the board into plain text and
``as_dict`` into the data the JSON and MCP surfaces carry, so every surface shows
the same numbers, and the numbers show only what the sample can support.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

from alpha_desk_research.agents.rating import RATING_REVIEW
from alpha_desk_research.memory.run_index import RunRecord
from alpha_desk_research.memory.scoring import RatingScore, alpha_of, score_entries

# The ways a row can be split. A caller asks for any of them; every one of them
# partitions the same sample, so the rows' counts always add up to the overall row.
DIMENSIONS = ("rating", "model", "provider", "analysts", "rounds", "ticker", "period", "horizon")

# Where the unattributable land, so the report says what it could not join instead
# of quietly crediting the run next door.
NO_RUN_RECORD = "unknown (no run record)"
NO_SETTINGS = "unknown (no settings)"

# The label of the whole-sample row, which is printed first and is not a dimension.
OVERALL = "overall"

# How a row whose label the log cannot give is printed: a pending entry has no
# holding window, so the horizon dimension's unlabelled row is an open decision,
# not a window; any other missing label is a gap in the log rather than a bucket.
UNSETTLED = "unsettled"
NO_LABEL = "unknown"


@dataclass
class GroupScore:
    """One row of the board: a dimension's value and how its decisions settled.

    ``count`` is every filtered decision filed under ``label``; ``resolved`` is the
    subset that was scored (settled, with a readable alpha, rating not ``REVIEW``),
    and ``hit_rate``/``mean_alpha`` describe only that subset. ``directional`` is
    the part of ``resolved`` that claimed a direction: the sample ``hit_rate`` is
    measured over and the count the printed rate's floor applies to, since
    ``resolved`` counts the ``Hold`` rows too and a rate cannot be computed from
    those. ``hit_rate`` is None exactly when ``directional`` is zero -- whether
    because nothing in the row claimed a direction or because nothing in it was
    scored -- and ``mean_alpha`` is None when nothing in it was scored.
    ``first``/``last`` are the trade dates the row's decisions span, which is the
    sample's range: pending rows are still part of it.
    """

    label: str | None
    count: int
    resolved: int
    pending: int
    unscored: int
    directional: int
    hit_rate: float | None
    mean_alpha: float | None
    first: str
    last: str


@dataclass
class Coverage:
    """What the log and the run index could not say about each other.

    ``runs_without_entry`` are recorded runs that decided nothing (or whose log
    entry is gone); ``entries_without_record`` are decisions no run claims, which
    is what an index-less install looks like. ``repeats`` are index lines that a
    later line for the same ``(trade_date, ticker)`` superseded -- a re-run.
    """

    runs_without_entry: int
    entries_without_record: int
    malformed_lines: int
    index_present: bool
    repeats: int


@dataclass
class Scoreboard:
    """The rated track record: a row per value of every dimension, plus the whole."""

    groups: dict[str, list[GroupScore]]
    overall: GroupScore
    coverage: Coverage
    window: str


def build_scoreboard(
    entries: list[dict],
    records: list[RunRecord],
    *,
    malformed: int = 0,
    index_present: bool = True,
    ticker: str | None = None,
    since: str | None = None,
    until: str | None = None,
    group_by: tuple[str, ...] | None = ("rating",),
) -> Scoreboard:
    """Join the log's decisions to the runs that produced them and score the result.

    ``entries`` are memory-log entries (``TradingMemoryLog.load_entries()``) and
    ``records`` run-index records (``RunIndex.load()``). ``ticker``/``since``/
    ``until`` filter both to the same slice -- the entry's trade date, inclusive --
    while ``malformed``/``index_present`` are passed through to ``coverage`` as the
    index reported them. ``group_by=None`` asks for the default rating dimension.

    Every dimension is scored, and ``group_by`` names the ones the caller leads
    with: the board is cheap to compute whole, and the surfaces read different
    dimensions from the same board (the CLI prints the ratings, the report card
    wants the ticker). A name outside ``DIMENSIONS`` is rejected rather than
    silently ignored.

    Only the last record for a ``(trade_date, ticker)`` is used, since a re-run
    replaces the settings it was made with; the ones it replaced are counted in
    ``coverage.repeats``. Rows keep the order their labels first appear in.
    """
    if group_by is None:
        group_by = ("rating",)
    requested = _dimensions(group_by)
    dimensions = requested + tuple(name for name in DIMENSIONS if name not in requested)
    since, until = _bound(since, "since"), _bound(until, "until")

    kept = [
        entry
        for entry in entries
        if _in_slice(entry.get("ticker"), entry.get("date"), ticker, since, until)
    ]
    by_key: dict[tuple, RunRecord] = {}
    repeats = 0
    for record in records:
        if not _in_slice(record.ticker, record.trade_date, ticker, since, until):
            continue
        key = (record.trade_date, record.ticker)
        if key in by_key:
            repeats += 1
        by_key[key] = record

    entry_keys = {_join_key(entry) for entry in kept}
    coverage = Coverage(
        runs_without_entry=sum(1 for key in by_key if key not in entry_keys),
        entries_without_record=sum(1 for entry in kept if _join_key(entry) not in by_key),
        malformed_lines=malformed,
        index_present=index_present,
        repeats=repeats,
    )

    def key_for(dimension: str) -> Callable[[dict], str | None]:
        def key(entry: dict) -> str | None:
            return _label(entry, by_key.get(_join_key(entry)), dimension)

        return key

    return Scoreboard(
        groups={name: _rows(kept, key_for(name)) for name in dimensions},
        overall=_score_row(OVERALL, kept, score_entries(kept, key=lambda _entry: OVERALL)),
        coverage=coverage,
        window=_window(kept),
    )


def render(
    scoreboard: Scoreboard,
    *,
    group_by: tuple[str, ...] = ("rating",),
    min_sample: int = 10,
) -> str:
    """The board as plain text: the overall row first, then the requested dimensions.

    ``group_by`` selects which of the board's dimensions to print, in order; the
    board carries all of them, so printing every one would make the choice
    meaningless. A name outside ``DIMENSIONS`` is rejected, as it is in
    ``build_scoreboard``.

    A rate is printed only when ``min_sample`` directional decisions stand behind
    it. A row whose settled decisions claimed no direction says so however many
    there are -- no sample size will ever rate it -- and any other row with too few
    directional decisions, including one where nothing has settled yet, says it
    cannot be rated. ``mean_alpha`` is shown either way, with the ``n`` it is over.

    This is the text, not the presentation: the CLI and the report decide how it
    looks, and both get the same lines from here.
    """
    lines = [_row_line(scoreboard.overall, OVERALL, scoreboard.window, min_sample)]
    for name in _dimensions(group_by):
        lines.append(f"{name}:")
        rows = sorted(scoreboard.groups.get(name, ()), key=lambda row: -row.count)
        lines.extend("  " + _row_line(row, name, scoreboard.window, min_sample) for row in rows)
        if not rows:
            lines.append("  no decisions")
    lines.extend(_coverage_lines(scoreboard.coverage))
    lines.extend(_sample_lines(scoreboard))
    return "\n".join(lines)


def as_dict(scoreboard: Scoreboard) -> dict:
    """The board as data, under the dataclasses' own field names.

    This is what ``--json`` and the MCP tool hand out, so it is the board itself
    rather than a summary of it: every dimension's rows, the overall row, the
    coverage counts and the window. Taking the shape from the dataclasses means a
    field added to a row reaches both surfaces without a second list to update.
    """
    return asdict(scoreboard)


def _dimensions(group_by: tuple[str, ...]) -> tuple[str, ...]:
    """The dimensions asked for, deduplicated and checked against ``DIMENSIONS``."""
    requested = tuple(dict.fromkeys(group_by))
    for name in requested:
        if name not in DIMENSIONS:
            raise ValueError(f"unknown group_by {name!r}; choose from {', '.join(DIMENSIONS)}")
    return requested


def _row_line(row: GroupScore, dimension: str, window: str, min_sample: int) -> str:
    """One printed row: what was decided, how it scored, over what, and when."""
    alpha = "n/a" if row.mean_alpha is None else f"{row.mean_alpha:+.2%}"
    span = f"{row.first}…{row.last}" if row.first else "no dates"
    return (
        f"{_label_text(dimension, row.label)}: n={row.count}, resolved={row.resolved}, "
        f"hit rate: {_rate(row, min_sample)}, mean alpha {alpha} (n={row.resolved}), "
        f"window {window}, {span}"
    )


def _rate(row: GroupScore, min_sample: int) -> str:
    """The row's rate, or why it has none.

    Settled decisions that claimed no direction have no rate however many of them
    there are, so they say so rather than looking like a small sample. Anything
    else short of ``min_sample`` *directional* decisions is only too thin to rate,
    which is also what a row where nothing has settled yet prints: there is no
    rate to show, whatever the floor.
    """
    if row.directional == 0 and row.resolved > 0:
        return "no direction claimed"
    if row.hit_rate is None or row.directional < min_sample:
        return "too few to rate"
    return f"{row.hit_rate:.0%}"


def _label_text(dimension: str, label: str | None) -> str:
    """A row's label as printed, with a label the log could not give spelled out.

    A pending entry has no ``holding``, so the horizon dimension holds a ``None``
    row; that is an open decision, not a window. Any other missing label is a gap
    in the log rather than a bucket, so it reads as unknown.
    """
    if label:
        return label
    return UNSETTLED if dimension == "horizon" else NO_LABEL


def _coverage_lines(coverage: Coverage) -> list[str]:
    """What the log and the index could not say about each other, when it is not none.

    A gap is reported once per kind; a clean join prints nothing, and an install
    with no index at all says so once rather than per decision.
    """
    lines = []
    if coverage.runs_without_entry:
        lines.append(
            "coverage: "
            + _count_line(
                coverage.runs_without_entry,
                "run recorded with no log entry",
                "runs recorded with no log entry",
            )
        )
    if coverage.entries_without_record:
        lines.append(
            "coverage: "
            + _count_line(
                coverage.entries_without_record,
                "decision with no run record",
                "decisions with no run record",
            )
        )
    if coverage.malformed_lines:
        lines.append(
            "coverage: "
            + _count_line(
                coverage.malformed_lines,
                "unreadable index line skipped",
                "unreadable index lines skipped",
            )
        )
    if coverage.repeats:
        lines.append(
            "coverage: "
            + _count_line(
                coverage.repeats,
                "re-run superseded (the last settings win)",
                "re-runs superseded (the last settings win)",
            )
        )
    if not coverage.index_present:
        lines.append("coverage: no run index, so nothing says what produced these decisions")
    return lines


def _sample_lines(scoreboard: Scoreboard) -> list[str]:
    """What the numbers are drawn from, and the one thing they cannot support.

    Decisions made on the same date share market beta, so the rows are not
    independent samples of the desk; the output says so rather than leaving a
    reader to infer more from the counts than they carry.
    """
    overall = scoreboard.overall
    span = f" from {overall.first} to {overall.last}" if overall.first else ""
    return [
        "sample: "
        + _count_line(overall.count, "decision", "decisions")
        + span
        + f" ({overall.resolved} resolved, {overall.pending} pending, {overall.unscored} unscored)",
        "decisions made on the same date share market beta, so the rows are not independent",
    ]


def _count_line(count: int, singular: str, plural: str) -> str:
    """A count with the phrase that fits it: ``1 run`` / ``2 runs``."""
    return f"{count} {singular if count == 1 else plural}"


def _rows(entries: list[dict], key: Callable[[dict], str | None]) -> list[GroupScore]:
    """One row per label ``key`` yields, in the order the labels first appear.

    The scores come from a single ``score_entries`` call, so a row's rate is the
    pool of its own entries -- each one judged against the direction *its* rating
    claimed -- and never an average of other rows' rates.
    """
    scores = score_entries(entries, key=key)
    grouped: dict[str | None, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(key(entry), []).append(entry)
    return [_score_row(label, group, scores) for label, group in grouped.items()]


def _score_row(
    label: str | None, group: list[dict], scores: dict[str | None, RatingScore]
) -> GroupScore:
    """Turn one label's entries and their pooled score into a row.

    ``scores`` has no entry for a label with nothing scored in it, which is a row
    of counts and no rate -- pending and ``REVIEW`` decisions are reported, never
    averaged in.
    """
    score = scores.get(label)
    dates = [date for date in (entry.get("date") for entry in group) if date]
    return GroupScore(
        label=label,
        count=len(group),
        resolved=score.count if score else 0,
        pending=sum(1 for entry in group if entry.get("pending")),
        unscored=sum(1 for entry in group if entry.get("rating") == RATING_REVIEW),
        directional=score.directional if score else 0,
        hit_rate=score.hit_rate if score else None,
        mean_alpha=score.mean_alpha if score else None,
        first=min(dates) if dates else "",
        last=max(dates) if dates else "",
    )


def _label(entry: dict, record: RunRecord | None, dimension: str) -> str | None:
    """What ``dimension`` says this entry is, from the entry or from its run record."""
    if dimension == "rating":
        return entry.get("rating")
    if dimension == "ticker":
        return entry.get("ticker")
    if dimension == "period":
        date = entry.get("date")
        return date[:7] if date else None
    if dimension == "horizon":
        return entry.get("holding")
    return _settings_label(record, dimension)


def _settings_label(record: RunRecord | None, dimension: str) -> str | None:
    """The setting that produced a run, or the bucket a run cannot be attributed to.

    A record with no settings -- missing, not a mapping, or empty -- and one whose
    settings lack the value this dimension needs both read as ``unknown (no
    settings)``: a default model is not evidence of which model ran.
    """
    if record is None:
        return NO_RUN_RECORD
    settings = record.settings
    if not isinstance(settings, dict) or not settings:
        return NO_SETTINGS
    if dimension == "analysts":
        analysts = settings.get("analysts")
        if isinstance(analysts, str):
            return analysts or NO_SETTINGS
        if isinstance(analysts, (list, tuple)) and analysts:
            return ",".join(str(analyst) for analyst in analysts)
        return NO_SETTINGS
    if dimension == "rounds":
        debate = settings.get("max_debate_rounds")
        risk = settings.get("max_risk_discuss_rounds")
        if debate is None or risk is None:
            return NO_SETTINGS
        return f"debate={debate},risk={risk}"
    value = settings.get("deep_think_llm" if dimension == "model" else "llm_provider")
    return NO_SETTINGS if value is None else str(value)


def _window(entries: list[dict]) -> str:
    """The holding windows the scored outcomes were measured over, as ``summarize()`` reports them.

    Read from the log's own ``holding`` values, never from a range this module
    computed: the window the outcomes actually used is the one to print.
    """
    resolved = [
        entry
        for entry in entries
        if not entry.get("pending")
        and entry.get("rating") != RATING_REVIEW
        and alpha_of(entry) is not None
    ]
    windows = {
        f"{(entry.get('holding') or '')[:-1]} trading days"
        for entry in resolved
        if (entry.get("holding") or "").endswith("d")
    }
    return ", ".join(sorted(windows)) or "the configured window"


def _join_key(entry: dict) -> tuple:
    """The key a log entry and a run record share: the cell the run was for."""
    return (entry.get("date"), entry.get("ticker"))


def _in_slice(
    ticker: str | None, date: str | None, wanted: str | None, since: str | None, until: str | None
) -> bool:
    """Whether one entity falls inside the report's slice. Dates compare as ISO strings.

    An entity with no date cannot be shown to be inside a dated window, so a
    caller who asks for one leaves it out.
    """
    if wanted is not None and ticker != wanted:
        return False
    if since is not None and (not date or str(date) < since):
        return False
    return until is None or (bool(date) and str(date) <= until)


def _bound(value: str | None, name: str) -> str | None:
    """A window bound as a canonical ``YYYY-MM-DD`` date, or None when unset."""
    if value is None:
        return None
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a YYYY-MM-DD date, got {value!r}") from exc
    if parsed.strftime("%Y-%m-%d") != str(value):
        raise ValueError(f"{name} must be a YYYY-MM-DD date, got {value!r}")
    return str(value)
