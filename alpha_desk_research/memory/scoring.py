"""Score a settled decision against the direction its rating claimed.

The memory log is the results table: every entry records the rating the desk made
and, once its holding window has traded, the alpha return it earned against the
instrument's regional benchmark. A rating is right when the sign of that alpha
agrees with the direction the rating claimed: Buy and Overweight expect alpha
above the benchmark, Underweight and Sell below it, and Hold claims no direction,
so nothing about alpha proves a Hold right or wrong.

The rule lives here rather than in the backtest that first used it, so every
reader of the log -- the backtest today, the scoreboard next -- scores settled
decisions through exactly one implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from alpha_desk_research.agents.rating import RATING_REVIEW


def alpha_of(entry: dict) -> float | None:
    """Alpha return of a settled entry, or None when it has not settled.

    The log stores it as a percentage rounded to one decimal, so aggregates here
    are accurate to 0.1 of a percentage point, not to the raw quote.
    """
    text = (entry.get("alpha") or "").strip().rstrip("%")
    try:
        return float(text) / 100
    except ValueError:
        return None


# What each rating claims will happen, so an outcome can be scored against it.
# Hold claims no direction, so nothing about alpha proves it right or wrong.
DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


@dataclass
class RatingScore:
    """One grouping's pool and how the calls in it did.

    ``count`` is the whole scored pool -- settled, with a readable alpha, not
    ``REVIEW`` -- and includes the entries whose rating claims no direction.
    ``directional`` is the subset of that pool that claimed one, which is the
    denominator ``hit_rate`` is measured over, so ``hit_rate`` is None exactly
    when it is zero. A reader that wants to know whether a rate's sample is big
    enough reads ``directional``, never ``count``.
    """

    count: int
    directional: int
    hit_rate: float | None
    mean_alpha: float


def score_entries(
    entries: list[dict], key: Callable[[dict], str | None] | None = None
) -> dict[str | None, RatingScore]:
    """Score settled entries, grouped by rating or by ``key(entry)``.

    An entry counts only if it has settled, carries a readable alpha, and is not
    a REVIEW: that rating is the sentinel for a decision nobody could read, so it
    claims no direction and can neither count for nor against the system. Groups
    keep the order in which they first appear.

    What an entry's alpha proves comes from that entry's own rating, never from
    the label it was grouped under, so a grouping other than the rating -- by
    model, provider or analyst, say -- still scores the calls it collects. An
    entry whose rating claims no direction (Hold, or a label outside the five
    tiers) stays in the group's count and mean alpha but carries no verdict, and
    the hit rate is the hits over the entries that do claim a direction; a group
    with none has no hit rate.
    """
    group = key or (lambda entry: entry["rating"])
    scored = [
        (group(entry), entry["rating"], alpha_of(entry))
        for entry in entries
        if not entry["pending"] and entry["rating"] != RATING_REVIEW
    ]
    scored = [(name, rating, alpha) for name, rating, alpha in scored if alpha is not None]
    scores: dict[str | None, RatingScore] = {}
    for name in dict.fromkeys(name for name, _, _ in scored):
        rows = [(rating, alpha) for group_name, rating, alpha in scored if group_name == name]
        hits = directionals = 0
        for rating, alpha in rows:
            direction = DIRECTION.get(rating, 0)
            if not direction:
                continue
            directionals += 1
            if alpha * direction > 0:
                hits += 1
        scores[name] = RatingScore(
            count=len(rows),
            directional=directionals,
            hit_rate=(hits / directionals) if directionals else None,
            mean_alpha=sum(alpha for _, alpha in rows) / len(rows),
        )
    return scores
