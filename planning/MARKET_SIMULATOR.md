# Market Simulator Design

## Approach and Code Structure for Simulating Stock Prices

Implements the `MarketDataSource` interface (`MARKET_INTERFACE.md`) as `backend/market/simulator.py`. Used whenever `MASSIVE_API_KEY` is unset (PLAN.md §5, §6 — the default path, since most users won't have a Massive key).

## 1. Requirements Recap (from PLAN.md §6)

- Prices follow geometric Brownian motion (GBM) with per-ticker drift/volatility.
- Updates at ~500ms intervals.
- Correlated moves across tickers (tech stocks move together).
- Occasional random "events" — sudden 2-5% moves on a ticker.
- Realistic seed prices for the 10 default tickers.
- New tickers (not in the seed table) get a deterministic price derived by hashing the ticker symbol into the $20-$400 range.
- Runs as an in-process background task, no external dependencies.
- `MARKET_SIM_SEED` seeds the RNG for full reproducibility (E2E tests).

## 2. Price Model — GBM Discretization

Continuous GBM: `dS = μS dt + σS dW`. Discretized per tick (Euler-Maruyama form, standard for this):

```
S(t+dt) = S(t) * exp( (μ - 0.5σ²)·dt + σ·√dt·Z )
```

where `Z ~ N(0, 1)` is a standard normal draw, `dt` is the tick interval in **years** (since `μ`/`σ` are conventionally annualized), and `μ`, `σ` are per-ticker drift and volatility.

```python
import math

def gbm_step(price: float, mu: float, sigma: float, dt_years: float, z: float) -> float:
    return price * math.exp((mu - 0.5 * sigma ** 2) * dt_years + sigma * math.sqrt(dt_years) * z)
```

With ticks every 500ms, `dt_years = 0.5 / (252 * 6.5 * 3600)` if you want "trading seconds" realism, or simply `0.5 / (365 * 24 * 3600)` for wall-clock realism — since this is a visual demo, not a research tool, wall-clock `dt` is simpler and avoids needing a trading-calendar/session model. Recommended: **use annualized `μ`/`σ` against wall-clock `dt`**, then tune the constants (§3) so the resulting per-tick moves *look* right rather than deriving them from first-principles market microstructure.

## 3. Per-Ticker Parameters and Seed Prices

```python
# backend/market/simulator_config.py
DEFAULT_TICKERS = {
    #        seed_price   mu (annual drift)  sigma (annual vol)  sector
    "AAPL":  (190.00,      0.12,               0.28,             "tech"),
    "GOOGL": (175.00,      0.10,               0.30,             "tech"),
    "MSFT":  (420.00,      0.11,               0.26,             "tech"),
    "AMZN":  (185.00,      0.13,               0.32,             "tech"),
    "TSLA":  (250.00,      0.05,               0.55,             "tesla"),   # own sector: idiosyncratic, high-vol
    "NVDA":  (130.00,      0.20,               0.45,             "tech"),
    "META":  (560.00,      0.14,               0.34,             "tech"),
    "JPM":   (210.00,      0.08,               0.22,             "finance"),
    "V":     (310.00,      0.09,               0.20,             "finance"),
    "NFLX":  (680.00,      0.10,               0.30,             "tech"),
}
```

`sigma` values are intentionally higher than real historical annualized volatility (typically 0.15-0.35 for these names) — a trading terminal demo needs *visible* price motion tick-to-tick; realistic institutional-grade volatility would look nearly flat over a typical demo session. Tune by eye against the "flash on every tick, visible drift over minutes" feel described in PLAN.md §2.

## 4. Deriving Seed Prices for Tickers Outside the Default Table

Per PLAN.md §6: "a ticker added later that has no predefined seed price... derives one deterministically by hashing the ticker symbol into a price in the $20-$400 range." Use a stable hash — **not** Python's builtin `hash()`, which is salted per-process (`PYTHONHASHSEED`) and would break reproducibility across runs/tests:

```python
import hashlib

def derive_seed_price(ticker: str) -> float:
    digest = hashlib.sha256(ticker.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 38_000   # 0..37999
    return 20.00 + bucket / 100             # $20.00 .. $399.99
```

New tickers also need `mu`/`sigma`/`sector` assigned — default to a flat, moderate profile (e.g. `mu=0.08, sigma=0.30, sector="general"`) since there's no real-world data to inform them, and put them in a sector bucket of their own (or a shared `"general"` bucket) rather than correlating them with tech/finance by default.

## 5. Correlated Moves — Single-Factor Model

To make "tech stocks move together" emerge naturally rather than hand-coding pairwise correlations, use a one-factor model per sector: each tick, draw one shared shock per sector, then blend it with each ticker's own idiosyncratic shock via a per-ticker `beta` (correlation strength to its sector factor).

```
Z_i = beta_i * Z_sector + sqrt(1 - beta_i²) * Z_idio_i
```

This keeps `Z_i` standard normal (variance-preserving) while making same-sector tickers move in the same direction most of the time, proportional to `beta_i`.

```python
import random

def correlated_shocks(rng: random.Random, tickers_by_sector: dict[str, list[str]], beta: float = 0.6) -> dict[str, float]:
    shocks: dict[str, float] = {}
    for sector, tickers in tickers_by_sector.items():
        z_sector = rng.gauss(0, 1)
        for ticker in tickers:
            z_idio = rng.gauss(0, 1)
            shocks[ticker] = beta * z_sector + math.sqrt(1 - beta ** 2) * z_idio
    return shocks
```

A single shared `beta=0.6` across all sectors is enough to visibly cluster tech-stock movement without a full covariance matrix — this is a demo aesthetic, not a risk model. `TSLA`'s dedicated single-ticker "sector" means it draws its own independent factor every tick (no correlation partner), matching its real-world reputation for idiosyncratic moves.

## 6. Random Events — Sudden 2-5% Moves

Each tick, independently per ticker, roll a small probability of an extra jump on top of the normal GBM step:

```python
EVENT_PROBABILITY_PER_TICK = 0.003   # ~ once every ~5-6 minutes per ticker at 500ms ticks
EVENT_MAGNITUDE_RANGE = (0.02, 0.05)  # 2%-5%

def maybe_apply_event(rng: random.Random, price: float) -> float:
    if rng.random() < EVENT_PROBABILITY_PER_TICK:
        magnitude = rng.uniform(*EVENT_MAGNITUDE_RANGE)
        direction = 1 if rng.random() < 0.5 else -1
        return price * (1 + direction * magnitude)
    return price
```

Applied after the GBM step, so an event tick still includes normal drift/vol plus the jump. Tune `EVENT_PROBABILITY_PER_TICK` so events are noticeable during a demo session (several minutes) without happening so often they stop feeling like "events."

## 7. RNG and Determinism

Use one `random.Random` instance owned by the simulator (never the global `random` module) so tests can construct a simulator with a fixed seed independent of anything else touching Python's global RNG state elsewhere in the process:

```python
class MarketSimulator(MarketDataSource):
    def __init__(self, cache: PriceCache, seed: int | None = None) -> None:
        self._cache = cache
        self._rng = random.Random(seed)   # seed=None → os-random seeding (normal, non-test runs)
        self._state: dict[str, TickerState] = {}
        self._tracked: set[str] = set()
        self._task: asyncio.Task | None = None
```

When `MARKET_SIM_SEED` is set, the *entire* subsequent tick sequence is reproducible: same seed → same sequence of `Z` draws → same prices at every tick count. This is what makes PLAN.md §12's deterministic E2E assertions (specific P&L values, flash colors) possible. Order of operations matters for reproducibility — always iterate tracked tickers in a fixed (e.g. sorted) order each tick, since dict/set iteration order otherwise depends on insertion history and could desync the RNG draw sequence across runs if tickers are added in a different order.

## 8. Per-Ticker State and the Tick Loop

```python
# backend/market/simulator.py
import asyncio
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.market.interface import MarketDataSource, PriceTick
from backend.market.cache import PriceCache
from backend.market.simulator_config import DEFAULT_TICKERS

TICK_INTERVAL_SECONDS = 0.5
DT_YEARS = TICK_INTERVAL_SECONDS / (365 * 24 * 3600)
SECTOR_BETA = 0.6


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
        self._rng = random.Random(seed)
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
            price, mu, sigma, sector = derive_seed_price(ticker), 0.08, 0.30, "general"
        return TickerState(price=price, prev_price=price, mu=mu, sigma=sigma, sector=sector)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run_loop(self) -> None:
        while True:
            self._tick()
            ticks = [
                PriceTick(
                    ticker=t,
                    price=s.price,
                    prev_price=s.prev_price,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    direction="up" if s.price > s.prev_price else "down" if s.price < s.prev_price else "flat",
                )
                for t, s in sorted(self._state.items())
                if t in self._tracked
            ]
            await self._cache.update_many(ticks)
            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    def _tick(self) -> None:
        by_sector: dict[str, list[str]] = {}
        for ticker in sorted(self._tracked):
            by_sector.setdefault(self._state[ticker].sector, []).append(ticker)

        shocks = correlated_shocks(self._rng, by_sector, beta=SECTOR_BETA)

        for ticker in sorted(self._tracked):
            state = self._state[ticker]
            z = shocks[ticker]
            new_price = gbm_step(state.price, state.mu, state.sigma, DT_YEARS, z)
            new_price = maybe_apply_event(self._rng, new_price)
            state.prev_price = state.price
            state.price = round(max(new_price, 0.01), 2)   # floor at 1 cent, avoid negative/zero prices
```

`gbm_step`, `correlated_shocks`, `maybe_apply_event`, and `derive_seed_price` are the pure functions from §2, §4, §5, §6 — kept separate from `MarketSimulator` so each is independently unit-testable (feed a fixed `rng`/inputs, assert exact output) without spinning up the async loop.

## 9. Testability

- **Pure functions, thin class.** `gbm_step`, `correlated_shocks`, `maybe_apply_event`, `derive_seed_price` take all their randomness as an explicit `rng`/`z` argument — no hidden state, easy to assert exact outputs in `pytest`.
- **Deterministic end-to-end.** With `MARKET_SIM_SEED` fixed, `MarketSimulator(cache, seed=N)` produces the exact same price sequence on every run, satisfying PLAN.md §12's requirement for reproducible E2E price/flash-color/P&L assertions.
- **No I/O.** The simulator never touches the network or disk — safe to run in unit tests with zero setup, and cheap to run in CI.

## 10. Non-Goals

- No attempt at real market microstructure (bid/ask spread modeling, order book depth, session/after-hours behavior). PLAN.md's market orders-only, fee-less design (§3) doesn't need it.
- No persistence of the simulated price path — it's regenerated fresh from `MARKET_SIM_SEED` (or unseeded) every process start, consistent with PLAN.md §10's note that no per-ticker price history is stored server-side.
- No cross-simulator-instance consistency — only one `MarketSimulator` runs per process (PLAN.md §3's single-container architecture), so there's no need to synchronize state across workers/replicas.
