"""Shared MarketDataSource contract, exercised against both real
implementations (planning/MARKET_DATA_DESIGN.md §11.2).
"""

import pytest

from backend.market.cache import PriceCache
from backend.market.interface import MarketDataSource
from backend.market.massive_client import MassiveMarketDataSource
from backend.market.simulator import MarketSimulator

SOURCE_FACTORIES = [
    lambda cache: MarketSimulator(cache, seed=1),
    lambda cache: MassiveMarketDataSource(cache, api_key="test-key", poll_interval=0.01),
]


@pytest.fixture(params=SOURCE_FACTORIES, ids=["simulator", "massive"])
def source(request) -> MarketDataSource:
    return request.param(PriceCache())


class TestMarketDataSourceConformance:
    def test_set_tracked_tickers_before_start_does_not_raise(self, source):
        source.set_tracked_tickers({"AAPL", "GOOGL"})

    def test_set_tracked_tickers_with_empty_set_does_not_raise(self, source):
        source.set_tracked_tickers(set())

    @pytest.mark.asyncio
    async def test_start_then_stop_does_not_raise(self, source):
        await source.start()
        await source.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_does_not_raise(self, source):
        await source.stop()

    @pytest.mark.asyncio
    async def test_start_is_safe_to_call_exactly_once(self, source):
        await source.start()
        await source.stop()
