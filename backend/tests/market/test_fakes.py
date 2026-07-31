import pytest

from backend.market.cache import PriceCache
from backend.market.interface import PriceTick
from backend.tests.fakes import FakeMarketDataSource


@pytest.mark.asyncio
async def test_start_populates_cache_with_fixed_ticks():
    cache = PriceCache()
    tick = PriceTick(ticker="AAPL", price=190.0, prev_price=189.0, timestamp="t", direction="up")
    source = FakeMarketDataSource(cache, {"AAPL": tick})

    await source.start()

    assert await cache.get("AAPL") == tick


@pytest.mark.asyncio
async def test_stop_does_not_raise():
    source = FakeMarketDataSource(PriceCache(), {})
    await source.stop()


def test_set_tracked_tickers_does_not_raise():
    source = FakeMarketDataSource(PriceCache(), {})
    source.set_tracked_tickers({"AAPL"})
