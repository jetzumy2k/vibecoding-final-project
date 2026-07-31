"""Test helpers for endpoint-level tests (SSE, trade execution, watchlist)
that need a MarketDataSource without exercising either real implementation.

See planning/MARKET_DATA_DESIGN.md §11.3.
"""

from backend.market.cache import PriceCache
from backend.market.interface import MarketDataSource, PriceTick


class FakeMarketDataSource(MarketDataSource):
    def __init__(self, cache: PriceCache, fixed_ticks: dict[str, PriceTick]) -> None:
        self._cache = cache
        self._fixed_ticks = fixed_ticks

    async def start(self) -> None:
        await self._cache.update_many(list(self._fixed_ticks.values()))

    async def stop(self) -> None:
        pass

    def set_tracked_tickers(self, tickers: set[str]) -> None:
        pass
