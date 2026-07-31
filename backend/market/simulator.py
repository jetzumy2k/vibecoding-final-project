"""In-process market simulator — geometric Brownian motion with correlated
sector shocks and occasional event jumps (planning/MARKET_SIMULATOR.md).

`gbm_step`, `correlated_shocks`, and `maybe_apply_event` are module-level
pure functions, kept separate from `MarketSimulator` so each is
independently unit-testable with a fixed `rng`/inputs.
"""

import asyncio
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.market.cache import PriceCache
from backend.market.interface import MarketDataSource, PriceTick
from backend.market.simulator_config import (
    DEFAULT_TICKERS,
    NEW_TICKER_MU,
    NEW_TICKER_SECTOR,
    NEW_TICKER_SIGMA,
    derive_seed_price,
)

TICK_INTERVAL_SECONDS = 0.5
DT_YEARS = TICK_INTERVAL_SECONDS / (365 * 24 * 3600)
SECTOR_BETA = 0.6
EVENT_PROBABILITY_PER_TICK = 0.003  # ~once every 5-6 min per ticker at 500ms ticks
EVENT_MAGNITUDE_RANGE = (0.02, 0.05)  # 2%-5%


def gbm_step(price: float, mu: float, sigma: float, dt_years: float, z: float) -> float:
    """Euler-Maruyama discretization of dS = μS dt + σS dW."""
    return price * math.exp((mu - 0.5 * sigma**2) * dt_years + sigma * math.sqrt(dt_years) * z)


def correlated_shocks(
    rng: random.Random, tickers_by_sector: dict[str, list[str]], beta: float = SECTOR_BETA
) -> dict[str, float]:
    """Single-factor model: one shared shock per sector blended with each
    ticker's idiosyncratic shock via `beta`. Variance-preserving (each
    resulting Z_i is still standard normal).
    """
    shocks: dict[str, float] = {}
    for sector, tickers in tickers_by_sector.items():
        z_sector = rng.gauss(0, 1)
        for ticker in tickers:
            z_idio = rng.gauss(0, 1)
            shocks[ticker] = beta * z_sector + math.sqrt(1 - beta**2) * z_idio
    return shocks


def maybe_apply_event(rng: random.Random, price: float) -> float:
    """Applied after the GBM step — a jump tick still includes the normal
    drift/vol move plus this jump."""
    if rng.random() < EVENT_PROBABILITY_PER_TICK:
        magnitude = rng.uniform(*EVENT_MAGNITUDE_RANGE)
        direction = 1 if rng.random() < 0.5 else -1
        return price * (1 + direction * magnitude)
    return price


@dataclass
class TickerState:
    price: float
    prev_price: float
    mu: float
    sigma: float
    sector: str


class MarketSimulator(MarketDataSource):
    def __init__(self, cache: PriceCache, seed: int | None = None) -> None:
        self._cache = cache
        self._rng = random.Random(seed)  # seed=None -> os-random seeding (normal runs)
        self._state: dict[str, TickerState] = {}
        self._tracked: set[str] = set()
        self._task: asyncio.Task | None = None

    def set_tracked_tickers(self, tickers: set[str]) -> None:
        self._tracked = set(tickers)
        for ticker in tickers:
            if ticker not in self._state:
                self._state[ticker] = self._new_ticker_state(ticker)
        for ticker in list(self._state):
            if ticker not in tickers:
                del self._state[ticker]

    def _new_ticker_state(self, ticker: str) -> TickerState:
        if ticker in DEFAULT_TICKERS:
            price, mu, sigma, sector = DEFAULT_TICKERS[ticker]
        else:
            price, mu, sigma, sector = (
                derive_seed_price(ticker),
                NEW_TICKER_MU,
                NEW_TICKER_SIGMA,
                NEW_TICKER_SECTOR,
            )
        return TickerState(price=price, prev_price=price, mu=mu, sigma=sigma, sector=sector)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while True:
            self._tick()
            await self._publish()
            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    async def _publish(self) -> None:
        ticks = [
            PriceTick(
                ticker=t,
                price=s.price,
                prev_price=s.prev_price,
                timestamp=datetime.now(timezone.utc).isoformat(),
                direction=PriceTick.compute_direction(s.price, s.prev_price),
            )
            for t, s in sorted(self._state.items())
            if t in self._tracked
        ]
        await self._cache.update_many(ticks)

    def _tick(self) -> None:
        if not self._tracked:
            return

        by_sector: dict[str, list[str]] = {}
        for ticker in sorted(self._tracked):
            by_sector.setdefault(self._state[ticker].sector, []).append(ticker)

        shocks = correlated_shocks(self._rng, by_sector, beta=SECTOR_BETA)

        for ticker in sorted(self._tracked):
            state = self._state[ticker]
            new_price = gbm_step(state.price, state.mu, state.sigma, DT_YEARS, shocks[ticker])
            new_price = maybe_apply_event(self._rng, new_price)
            state.prev_price = state.price
            state.price = round(max(new_price, 0.01), 2)  # floor at 1 cent
