"""Spec §1g: router vendor cache — second identical call avoids refetch."""

from unittest import mock

import pytest

from tradingagents.dataflows import router


@pytest.mark.unit
def test_vendor_cache_hit_avoids_refetch():
    fake = mock.Mock(return_value="Date,Open\n2024-01-01,1.0\n")
    with mock.patch.dict(
        router.VENDOR_METHODS,
        {"get_stock_data": {"yfinance": fake}},
        clear=False,
    ):
        first = router.route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2024-01-10")
        second = router.route_to_vendor("get_stock_data", "AAPL", "2024-01-01", "2024-01-10")
    assert first == second
    assert fake.call_count == 1
