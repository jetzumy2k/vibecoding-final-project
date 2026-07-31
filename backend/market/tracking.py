"""Tracked ticker set = watchlist ∪ open positions (planning/PLAN.md §6).

Recomputed after every mutation that could change the union (watchlist
add/remove, trade execution) and pushed into the active MarketDataSource.
"""

from typing import Protocol

from backend.market.interface import MarketDataSource


class TrackedTickerSource(Protocol):
    async def get_watchlist_tickers(self, user_id: str) -> list[str]: ...

    async def get_open_position_tickers(self, user_id: str) -> list[str]: ...


async def recompute_tracked_tickers(
    db: TrackedTickerSource, market_data_source: MarketDataSource, user_id: str = "default"
) -> None:
    watchlist_tickers = await db.get_watchlist_tickers(user_id)
    position_tickers = await db.get_open_position_tickers(user_id)
    market_data_source.set_tracked_tickers(set(watchlist_tickers) | set(position_tickers))
