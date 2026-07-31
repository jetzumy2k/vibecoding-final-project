"""Unified market data interface.

Implemented by both `MarketSimulator` and `MassiveMarketDataSource` so that
SSE streaming, trade execution, and portfolio valuation never import or
branch on which source is active (planning/MARKET_DATA_DESIGN.md §3).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceTick:
    ticker: str
    price: float
    prev_price: float
    timestamp: str  # ISO 8601 UTC, e.g. "2026-07-29T14:32:01.123Z"
    direction: str  # "up" | "down" | "flat" — derived from price vs. prev_price

    @staticmethod
    def compute_direction(price: float, prev_price: float) -> str:
        if price > prev_price:
            return "up"
        if price < prev_price:
            return "down"
        return "flat"


class MarketDataSource(ABC):
    """Implemented by both MarketSimulator and MassiveMarketDataSource.

    A source owns exactly one background polling/generation loop and writes
    every tick into the PriceCache passed to it at construction. It never
    reads from the cache itself — reads are the caller's job (SSE, REST).
    """

    @abstractmethod
    async def start(self) -> None:
        """Begin the background loop. Must be safe to call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background loop and release resources (HTTP clients, etc.)."""

    @abstractmethod
    def set_tracked_tickers(self, tickers: set[str]) -> None:
        """Replace the tracked ticker set. Implementations pick up the new
        set on their next tick/poll — no restart required."""
