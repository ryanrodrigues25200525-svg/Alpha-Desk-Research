"""Spec §1c: routed vendor output caps (no network; oversized Fake payloads)."""

from unittest import mock

import pytest

from tradingagents.dataflows import router


def _route(vendors_for_get_stock_data):
    return mock.patch.dict(
        router.VENDOR_METHODS,
        {"get_stock_data": vendors_for_get_stock_data},
        clear=False,
    )


def _csv_rows(n):
    header = "Date,Open,High,Low,Close,Volume\n"
    return header + "".join(
        f"2024-01-{i:02d},{i}.0,{i}.0,{i}.0,{i}.0,100\n" for i in range(1, n + 1)
    )


@pytest.mark.unit
def test_ohlcv_capped_to_30_rows():
    big = "# Total records: 60\n" + _csv_rows(60)
    with _route({"yfinance": mock.Mock(return_value=big)}):
        out = router.route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2024-06-01")
    lines = [ln for ln in out.split("\n") if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 31  # header + last 30 rows
    assert lines[1].startswith("2024-01-31")
    assert "# Total records: 30" in out


@pytest.mark.unit
def test_statements_keep_two_most_recent_periods():
    stmt = (
        "Breakdown,2024-01-01,2023-01-01,2022-01-01,2021-01-01\n"
        "Assets,1,2,3,4\n"
    )
    with mock.patch.dict(
        router.VENDOR_METHODS,
        {"get_balance_sheet": {"yfinance": mock.Mock(return_value=stmt)}},
        clear=False,
    ):
        out = router.route_to_vendor("get_balance_sheet", "AAPL", "quarterly", "2024-06-01")
    header = [ln for ln in out.split("\n") if ln.strip() and not ln.startswith("#")][0]
    assert header == "Breakdown,2024-01-01,2023-01-01"


@pytest.mark.unit
def test_news_capped_at_configured_limit():
    arts = "".join(f"### Article {i}\nbody {i}\n" for i in range(20))
    with mock.patch.dict(
        router.VENDOR_METHODS,
        {"get_news": {"yfinance": mock.Mock(return_value="Intro\n" + arts)}},
        clear=False,
    ):
        out = router.route_to_vendor("get_news", "AAPL", "2024-01-01", "2024-06-01")
    assert out.count("### ") == router.get_config().get("news_article_limit", 8)
