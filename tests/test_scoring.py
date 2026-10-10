# tests/test_scoring.py
from alpha_desk_research.memory.scoring import DIRECTION, RatingScore, alpha_of, score_entries


def _entry(rating, alpha, pending=False):
    return {"date": "2026-01-05", "ticker": "NVDA", "rating": rating,
            "pending": pending, "alpha": alpha, "holding": "5d"}

def test_a_bearish_call_that_fell_is_a_hit():
    scored = score_entries([_entry("Sell", "-4.0%")])["Sell"]
    assert isinstance(scored, RatingScore)
    assert scored.hit_rate == 1.0

def test_hold_has_no_hit_rate():
    assert score_entries([_entry("Hold", "+9.0%")])["Hold"].hit_rate is None

def test_review_and_pending_are_never_scored():
    assert score_entries([_entry("REVIEW", "+1.0%"), _entry("Buy", "+1.0%", pending=True)]) == {}

def test_a_non_numeric_alpha_is_excluded_not_fatal():
    assert alpha_of(_entry("Buy", "n/a")) is None
    assert score_entries([_entry("Buy", "n/a")]) == {}

def test_direction_covers_all_five_tiers():
    assert DIRECTION == {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}

def test_a_non_rating_grouping_still_scores():
    entries = [_entry("Buy", "+2.0%"), _entry("Buy", "-1.0%"), _entry("Sell", "+1.0%")]
    by_ticker = score_entries(entries, key=lambda e: e["ticker"])
    assert by_ticker["NVDA"].hit_rate == 1 / 3   # two Buys: one hit; one Sell: a miss
    assert by_ticker["NVDA"].count == 3
