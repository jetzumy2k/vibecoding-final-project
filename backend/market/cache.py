"""Shared in-memory price cache.

One writer (the active MarketDataSource's background loop), many readers
(SSE connections, /api/portfolio, /api/watchlist, trade execution).
asyncio.Lock is sufficient — single event loop, single process, single
user (planning/PLAN.md §3, §7); no multi-worker concerns.
"""

import asyncio

from backend.market.interface import PriceTick


class PriceCache:
    def __init__(self) -> None:
        self._ticks: dict[str, PriceTick] = {}
        self._lock = asyncio.Lock()

    async def update(self, tick: PriceTick) -> None:
        async with self._lock:
            self._ticks[tick.ticker] = tick

    async def update_many(self, ticks: list[PriceTick]) -> None:
        async with self._lock:
            for tick in ticks:
                self._ticks[tick.ticker] = tick

    async def get(self, ticker: str) -> PriceTick | None:
        async with self._lock:
            return self._ticks.get(ticker)

    async def get_all(self) -> dict[str, PriceTick]:
        async with self._lock:
            return dict(self._ticks)
