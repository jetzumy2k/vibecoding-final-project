# Market Data Backend — Consolidated Design

## Unified API, Simulator, and Massive Implementation

This document is the single, implementation-ready reference for FinAlly's market data backend (`backend/market/`). It consolidates and extends `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, and `MASSIVE.md` into one coherent design with complete, buildable code — including the `MassiveMarketDataSource` polling class, which the source-specific docs described in pieces (HTTP fetch, response parsing) but never assembled into the `MarketDataSource` contract itself. Read this doc top to bottom to implement `backend/market/` in one pass; the three source docs remain as deeper reference material for the reasoning behind specific choices (GBM math, correlation model, Massive API research notes).

Implements PLAN.md §6 ("Market Data") and feeds PLAN.md §7 (positions/trades tables), §8 (`/api/stream/prices`, `/api/portfolio/trade`), and §12 (deterministic E2E tests via `MARKET_SIM_SEED` / `LLM_MOCK`).

---

## 1. Goals & Constraints

- **One interface, two interchangeable implementations.** SSE streaming, trade execution, and portfolio valuation never import or branch on `MarketSimulator` vs. `MassiveMarketDataSource` — they only ever touch `PriceCache`.
- **One writer per process.** Exactly one `MarketDataSource` is active (decided once at startup by `MASSIVE_API_KEY` presence), and it owns the only background loop writing into the shared cache.
- **Dynamic tracked-ticker set.** Tracked tickers = watchlist ∪ open positions (PLAN.md §6), recomputed on every watchlist/trade mutation, pushed into the active source via `set_tracked_tickers()` without restarting anything.
- **Deterministic where it matters.** `MARKET_SIM_SEED` makes the simulator's entire tick sequence reproducible for E2E tests (PLAN.md §12). Massive is inherently non-deterministic (live market data) and is never used in the deterministic E2E path.
- **No historical persistence.** Neither implementation stores per-ticker history server-side; sparklines/charts are built client-side from SSE ticks (PLAN.md §10).
- **Fail soft, log loud.** A transient Massive error (rate limit, network blip) must not crash the process or blank out the cache — it should log and retain the last known good prices until the next successful poll.

---

## 2. Module Layout

```
backend/
└── market/
    ├── __init__.py           # exports create_market_data_source(), PriceCache, PriceTick, MarketDataSource
    ├── interface.py           # MarketDataSource ABC, PriceTick dataclass
    ├── cache.py                # PriceCache — the shared in-memory store
    ├── tracking.py             # recompute_tracked_tickers(db, source)
    ├── simulator.py            # MarketSimulator(MarketDataSource)
    ├── simulator_config.py     # DEFAULT_TICKERS, derive_seed_price()
    ├── massive_client.py       # MassiveMarketDataSource(MarketDataSource), httpx fetch + parse
    └── factory.py               # create_market_data_source(): env-var based selection
```

Nothing outside `backend/market/` imports `simulator.py` or `massive_client.py` directly — only `factory.py` does, and only `factory.create_market_data_source()` is imported elsewhere (FastAPI startup).

---

## 3. Core Types — `interface.py`

```python
# backend/market/interface.py
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceTick:
    ticker: str
    price: float
    prev_price: float
    timestamp: str      # ISO 8601 UTC, e.g. "2026-07-29T14:32:01.123Z"
    direction: str        # "up" | "down" | "flat" — derived from price vs. prev_price

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
```

---

## 4. Shared Price Cache — `cache.py`

```python
# backend/market/cache.py
import asyncio
from backend.market.interface import PriceTick


class PriceCache:
    """Single shared in-memory store of the latest tick per ticker.

    One writer (the active MarketDataSource's background loop), many readers
    (SSE connections, /api/portfolio, /api/watchlist, trade execution).
    asyncio.Lock is sufficient — single event loop, single process, single
    user (PLAN.md §3, §7); no multi-worker concerns.
    """

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
```

This satisfies PLAN.md §6's "Shared Price Cache" requirement verbatim: latest price, previous price, timestamp per ticker; SSE and REST both read from it independently of whichever source is writing.

---

## 5. Tracked Ticker Set — `tracking.py`

```python
# backend/market/tracking.py
from backend.market.interface import MarketDataSource


async def recompute_tracked_tickers(db, market_data_source: MarketDataSource, user_id: str = "default") -> None:
    watchlist_tickers = await db.get_watchlist_tickers(user_id)
    position_tickers = await db.get_open_position_tickers(user_id)
    market_data_source.set_tracked_tickers(set(watchlist_tickers) | set(position_tickers))
```

**Call sites** (must call this after every mutation that could change the union):

| Endpoint | Why |
|---|---|
| `POST /api/watchlist` | Adds a ticker to the union |
| `DELETE /api/watchlist/{ticker}` | Removes a ticker *unless* an open position still holds it |
| `POST /api/portfolio/trade` | A buy can open a new position (adds to union); a sell can close one to zero (may remove from union if not also watchlisted) |
| Auto-executed LLM trades/watchlist changes (§9 of PLAN.md) | Same reasons as the manual endpoints — call once after each chat-triggered action, not per parsed intent |

Each `MarketDataSource` implementation handles a tracked-set update differently but honors the same contract (fully replaced set, reflected by the next tick/poll):

- **Simulator**: allocates GBM state for newly-tracked tickers (seeding via `DEFAULT_TICKERS` or the hash-derivation fallback), drops state for no-longer-tracked tickers.
- **Massive**: just changes the `tickers` CSV param used on the next poll — no per-ticker state to manage.

---

## 6. Simulator — `simulator_config.py` + `simulator.py`

### 6.1 Seed prices, drift, volatility, sector

```python
# backend/market/simulator_config.py
import hashlib

DEFAULT_TICKERS = {
    #        seed_price   mu (annual drift)  sigma (annual vol)  sector
    "AAPL":  (190.00,      0.12,               0.28,             "tech"),
    "GOOGL": (175.00,      0.10,               0.30,             "tech"),
    "MSFT":  (420.00,      0.11,               0.26,             "tech"),
    "AMZN":  (185.00,      0.13,               0.32,             "tech"),
    "TSLA":  (250.00,      0.05,               0.55,             "tesla"),   # own sector: idiosyncratic
    "NVDA":  (130.00,      0.20,               0.45,             "tech"),
    "META":  (560.00,      0.14,               0.34,             "tech"),
    "JPM":   (210.00,      0.08,               0.22,             "finance"),
    "V":     (310.00,      0.09,               0.20,             "finance"),
    "NFLX":  (680.00,      0.10,               0.30,             "tech"),
}

NEW_TICKER_MU = 0.08
NEW_TICKER_SIGMA = 0.30
NEW_TICKER_SECTOR = "general"


def derive_seed_price(ticker: str) -> float:
    """Deterministic $20.00-$399.99 seed price for tickers outside DEFAULT_TICKERS.

    Uses sha256, not Python's builtin hash() — hash() is salted per-process
    (PYTHONHASHSEED) and would break reproducibility across runs/tests.
    """
    digest = hashlib.sha256(ticker.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 38_000   # 0..37999
    return 20.00 + bucket / 100
```

`sigma` values here run higher than real historical annualized volatility (typically 0.15-0.35) — a terminal demo needs *visible* tick-to-tick motion; institutionally-realistic vol looks flat over a demo session. New tickers default to a flat `mu=0.08, sigma=0.30` in their own `"general"` sector bucket (no correlation partner) since there's no real data to derive parameters from.

### 6.2 Price model — GBM discretization

Continuous GBM: `dS = μS dt + σS dW`. Euler-Maruyama discretization per tick:

```
S(t+dt) = S(t) · exp( (μ − 0.5σ²)·dt + σ·√dt·Z )     where Z ~ N(0,1)
```

```python
import math


def gbm_step(price: float, mu: float, sigma: float, dt_years: float, z: float) -> float:
    return price * math.exp((mu - 0.5 * sigma ** 2) * dt_years + sigma * math.sqrt(dt_years) * z)
```

`dt_years` uses wall-clock time (`0.5s / (365·24·3600)`), not a trading-calendar/session model — this is a visual demo, not a research tool, so `mu`/`sigma` are tuned by eye against wall-clock ticks rather than derived from market microstructure first principles.

### 6.3 Correlated moves — single-factor sector model

"Tech stocks move together" emerges from a one-factor model: each tick, draw one shared shock per sector, blend it with each ticker's idiosyncratic shock via `beta` (correlation strength):

```
Z_i = beta · Z_sector + sqrt(1 − beta²) · Z_idio_i
```

This is variance-preserving (`Z_i` stays standard normal) while clustering same-sector moves.

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

A single shared `beta=0.6` is enough to visibly cluster tech-sector movement without a full covariance matrix. `TSLA`'s dedicated single-ticker `"tesla"` sector means it always draws its own independent factor (no correlation partner), matching its idiosyncratic real-world reputation.

### 6.4 Random events — sudden 2-5% moves

```python
EVENT_PROBABILITY_PER_TICK = 0.003    # ~once every 5-6 min per ticker at 500ms ticks
EVENT_MAGNITUDE_RANGE = (0.02, 0.05)   # 2%-5%


def maybe_apply_event(rng: random.Random, price: float) -> float:
    if rng.random() < EVENT_PROBABILITY_PER_TICK:
        magnitude = rng.uniform(*EVENT_MAGNITUDE_RANGE)
        direction = 1 if rng.random() < 0.5 else -1
        return price * (1 + direction * magnitude)
    return price
```

Applied *after* the GBM step, so an event tick still includes normal drift/vol plus the jump.

### 6.5 RNG ownership and determinism

The simulator owns one `random.Random` instance — never the global `random` module — so a fixed `MARKET_SIM_SEED` produces a fully reproducible tick sequence regardless of anything else touching global RNG state elsewhere in the process. Tickers are always iterated in **sorted order** each tick so the RNG draw sequence doesn't desync based on insertion history (dict/set order otherwise depends on when tickers were added).

### 6.6 Full implementation

```python
# backend/market/simulator.py
import asyncio
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.market.interface import MarketDataSource, PriceTick
from backend.market.cache import PriceCache
from backend.market.simulator_config import (
    DEFAULT_TICKERS,
    NEW_TICKER_MU,
    NEW_TICKER_SIGMA,
    NEW_TICKER_SECTOR,
    derive_seed_price,
)

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
        self._rng = random.Random(seed)   # seed=None -> os-random seeding (normal runs)
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
            price, mu, sigma, sector = derive_seed_price(ticker), NEW_TICKER_MU, NEW_TICKER_SIGMA, NEW_TICKER_SECTOR
        return TickerState(price=price, prev_price=price, mu=mu, sigma=sigma, sector=sector)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while True:
            self._tick()
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
            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    def _tick(self) -> None:
        by_sector: dict[str, list[str]] = {}
        for ticker in sorted(self._tracked):
            by_sector.setdefault(self._state[ticker].sector, []).append(ticker)

        shocks = correlated_shocks(self._rng, by_sector, beta=SECTOR_BETA)

        for ticker in sorted(self._tracked):
            state = self._state[ticker]
            new_price = gbm_step(state.price, state.mu, state.sigma, DT_YEARS, shocks[ticker])
            new_price = maybe_apply_event(self._rng, new_price)
            state.prev_price = state.price
            state.price = round(max(new_price, 0.01), 2)   # floor at 1 cent
```

`gbm_step`, `correlated_shocks`, `maybe_apply_event` are module-level pure functions (§6.2-§6.4) — kept separate from the class so each is independently unit-testable with a fixed `rng`/inputs.

---

## 7. Massive API — `massive_client.py`

Massive is a straight rebrand of Polygon.io (endpoints/schemas/keys unchanged); FinAlly targets the new `api.massive.com` host. Full API research lives in `MASSIVE.md` — this section assembles that research into the `MarketDataSource`-conforming class.

### 7.1 HTTP fetch and response parsing

```python
import httpx
from datetime import datetime, timezone

MASSIVE_BASE_URL = "https://api.massive.com"


async def fetch_snapshot(client: httpx.AsyncClient, tickers: list[str], api_key: str) -> dict:
    resp = await client.get(
        f"{MASSIVE_BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers",
        params={"tickers": ",".join(tickers), "apiKey": api_key},
    )
    resp.raise_for_status()
    return resp.json()


def parse_snapshot(payload: dict) -> dict[str, dict]:
    """Returns {ticker: {"price": float, "prev_price": float, "timestamp": str}}.

    An unknown ticker is simply absent from payload["tickers"] rather than
    erroring the whole batch (Massive's v2 snapshot drops unrecognized
    symbols) — callers must tolerate a returned dict smaller than requested.
    """
    out: dict[str, dict] = {}
    for item in payload.get("tickers", []):
        ticker = item["ticker"]
        price = item["lastTrade"]["p"]
        prev_price = item.get("prevDay", {}).get("c", price)
        ts_ns = item["lastTrade"]["t"]
        out[ticker] = {
            "price": price,
            "prev_price": prev_price,
            "timestamp": datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).isoformat(),
        }
    return out
```

One request returns the entire tracked ticker set (batched snapshot), so call volume never scales with watchlist size — this is what makes the free tier's 5 calls/min ceiling workable.

### 7.2 Poll interval selection

```python
FREE_TIER_POLL_SECONDS = 15.0     # free tier: 5 req/min hard ceiling -> stay under it
PAID_TIER_POLL_SECONDS = 5.0       # paid tiers: unlimited calls, gated by data recency instead

MASSIVE_POLL_INTERVAL_SECONDS = float(
    os.environ.get("MASSIVE_POLL_INTERVAL_SECONDS", FREE_TIER_POLL_SECONDS)
)
```

FinAlly has no reliable way to detect the caller's Massive plan tier from the API itself, so the poll interval defaults conservatively to the free-tier-safe 15s and is overridable via an (undocumented-in-PLAN.md, power-user) env var for anyone on a paid plan who wants faster refresh. This keeps the default safe without hardcoding a single interval that's wrong for half of users.

### 7.3 Error handling

Per `MASSIVE.md` §7:

| Condition | Handling |
|---|---|
| `200` + `"status": "OK"` or `"DELAYED"` | Normal — parse and cache |
| `401` / `403` | Misconfigured or under-entitled key — log once clearly, keep retrying on the normal interval (don't crash the process; the user can fix `.env` and restart) |
| `404` on individual tickers | Not a whole-request failure — `parse_snapshot` already tolerates a shorter-than-requested result |
| `429` | Rate limited — back off: skip this poll, wait, retry next interval. Do not tighten the loop by retrying immediately |
| Network error / timeout | Log, keep last-known-good cache values, retry next interval |

The cache is never cleared on a failed poll — stale-but-present prices are strictly better for the UI than a blank watchlist, and the next successful poll overwrites them.

### 7.4 Full implementation

```python
# backend/market/massive_client.py
import asyncio
import logging
import os
import httpx

from backend.market.interface import MarketDataSource, PriceTick
from backend.market.cache import PriceCache

logger = logging.getLogger(__name__)

MASSIVE_BASE_URL = "https://api.massive.com"
FREE_TIER_POLL_SECONDS = 15.0
MAX_CONSECUTIVE_ERROR_LOGS = 1   # log the first error in a failure streak verbosely, then quiet down


class MassiveMarketDataSource(MarketDataSource):
    def __init__(self, cache: PriceCache, api_key: str, poll_interval: float | None = None) -> None:
        self._cache = cache
        self._api_key = api_key
        self._poll_interval = poll_interval or float(
            os.environ.get("MASSIVE_POLL_INTERVAL_SECONDS", FREE_TIER_POLL_SECONDS)
        )
        self._tracked: set[str] = set()
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._prev_prices: dict[str, float] = {}   # fallback prev_price when prevDay.c is stale/missing
        self._consecutive_errors = 0

    def set_tracked_tickers(self, tickers: set[str]) -> None:
        self._tracked = set(tickers)

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=10.0)
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    async def _run_loop(self) -> None:
        while True:
            if self._tracked:
                await self._poll_once()
            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        tickers = sorted(self._tracked)
        try:
            payload = await fetch_snapshot(self._client, tickers, self._api_key)
        except httpx.HTTPStatusError as exc:
            self._log_error(f"Massive HTTP {exc.response.status_code} for {tickers}: {exc}")
            return
        except httpx.HTTPError as exc:
            self._log_error(f"Massive request failed for {tickers}: {exc}")
            return

        self._consecutive_errors = 0
        parsed = parse_snapshot(payload)
        missing = set(tickers) - parsed.keys()
        if missing:
            logger.warning("Massive snapshot returned no data for: %s", sorted(missing))

        ticks = []
        for ticker, data in parsed.items():
            price = data["price"]
            prev_price = self._prev_prices.get(ticker, data["prev_price"])
            ticks.append(
                PriceTick(
                    ticker=ticker,
                    price=price,
                    prev_price=prev_price,
                    timestamp=data["timestamp"],
                    direction=PriceTick.compute_direction(price, prev_price),
                )
            )
            self._prev_prices[ticker] = price

        await self._cache.update_many(ticks)

    def _log_error(self, message: str) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors <= MAX_CONSECUTIVE_ERROR_LOGS:
            logger.error(message)
        else:
            logger.debug(message)   # avoid log-spamming on sustained outages
```

Notes on design choices not spelled out in `MASSIVE.md`:

- **`_prev_prices` overrides `prevDay.c` after the first successful poll.** Massive's `prevDay.c` is yesterday's close and doesn't change intra-poll — using it as `prev_price` on every tick would make the *first* poll's flash direction meaningful but every subsequent poll's flash direction compare against yesterday's close instead of the last poll, which is wrong for a "did it just tick up or down" UI signal. After the first poll, `prev_price` is always the previous poll's price for that ticker.
- **Errors don't stop the loop.** `_poll_once` catches and logs; `_run_loop` always re-sleeps and retries. A sustained Massive outage degrades to stale prices, never a crash.
- **Log de-duplication** (`_consecutive_errors`) avoids flooding logs during a multi-minute outage while still surfacing the first failure loudly.

---

## 8. Factory — `factory.py`

```python
# backend/market/factory.py
import os
from backend.market.cache import PriceCache
from backend.market.interface import MarketDataSource
from backend.market.simulator import MarketSimulator
from backend.market.massive_client import MassiveMarketDataSource


def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveMarketDataSource(cache=cache, api_key=api_key)

    seed = os.environ.get("MARKET_SIM_SEED")
    return MarketSimulator(cache=cache, seed=int(seed) if seed else None)
```

Selection happens **once**, at process startup — there is no runtime switching between simulator and Massive. `MARKET_SIM_SEED` is read only on the simulator branch; it's inert when Massive is active (PLAN.md §5: "ignored when `MASSIVE_API_KEY` is set").

---

## 9. FastAPI Integration

### 9.1 Startup / shutdown wiring

```python
# backend/main.py (excerpt)
from backend.market.cache import PriceCache
from backend.market.factory import create_market_data_source

price_cache = PriceCache()
market_data_source = create_market_data_source(price_cache)


@app.on_event("startup")
async def startup() -> None:
    await market_data_source.start()
    # Initial tracked-set population from seeded/default watchlist:
    from backend.market.tracking import recompute_tracked_tickers
    await recompute_tracked_tickers(db, market_data_source)


@app.on_event("shutdown")
async def shutdown() -> None:
    await market_data_source.stop()
```

### 9.2 SSE streaming — `/api/stream/prices`

The SSE endpoint never touches `MarketDataSource` — only the cache — decoupling client-facing push cadence from the source's generation/polling cadence. This matters because the simulator ticks every 500ms but Massive on the free tier only refreshes every 15s; SSE still pushes at a steady interval, simply re-sending unchanged values between Massive polls.

```python
# backend/api/stream.py (excerpt)
import asyncio
import json
from fastapi import Request
from fastapi.responses import StreamingResponse

SSE_PUSH_INTERVAL_SECONDS = 0.5


async def price_stream(request: Request, cache: PriceCache):
    async def event_generator():
        while not await request.is_disconnected():
            ticks = await cache.get_all()
            for tick in ticks.values():
                payload = {
                    "ticker": tick.ticker,
                    "price": tick.price,
                    "prev_price": tick.prev_price,
                    "timestamp": tick.timestamp,
                    "direction": tick.direction,
                }
                yield f"event: price\ndata: {json.dumps(payload)}\n\n"
            await asyncio.sleep(SSE_PUSH_INTERVAL_SECONDS)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

### 9.3 Trade execution — reads fill price from the cache, never the client

```python
# backend/api/portfolio.py (excerpt)
async def execute_trade(ticker: str, side: str, quantity: float, cache: PriceCache, db) -> dict:
    tick = await cache.get(ticker)
    if tick is None:
        raise TickerNotTrackedError(ticker)   # -> 400/404; ticker isn't in watchlist ∪ positions
    fill_price = tick.price
    # ... validate cash (buy) / shares held (sell), write positions + trades rows using fill_price ...
    from backend.market.tracking import recompute_tracked_tickers
    await recompute_tracked_tickers(db, market_data_source)   # position may have opened/closed
    return result
```

Satisfies PLAN.md §8's explicit requirement: fill price is read from the server-side shared price cache at request time, never a client-supplied or client-cached price.

### 9.4 Watchlist endpoints

`POST /api/watchlist` and `DELETE /api/watchlist/{ticker}` both call `recompute_tracked_tickers()` after mutating the `watchlist` table (§5 above). A `DELETE` on a ticker with an open position leaves it tracked (union with `positions`), so its price keeps streaming and P&L stays accurate per PLAN.md §6/§8.

---

## 10. Dependencies

`backend/pyproject.toml` additions beyond FastAPI/uvicorn/pydantic:

```toml
[project]
dependencies = [
    "httpx>=0.27",   # Massive API client — also usable for LiteLLM/OpenRouter calls
]
```

No SDK dependency for Massive (`massive`/`polygon-api-client` packages) — `MASSIVE.md` §6 found the SDK's batch-snapshot method name unverified in its docs, so FinAlly calls the REST endpoint directly via `httpx`, matching the simulator's zero-magic, fully-inspectable style.

---

## 11. Testing Strategy

### 11.1 Unit tests (`backend/tests/`)

- **Simulator pure functions** — `gbm_step`, `correlated_shocks`, `maybe_apply_event`, `derive_seed_price`: fixed `rng`/inputs, assert exact numeric output. No async, no I/O.
- **Simulator class** — construct with `seed=N`, call `set_tracked_tickers`, step the loop manually (call `_tick()` directly rather than `start()`), assert the cache receives expected ticks; assert two simulators constructed with the same seed produce identical price sequences.
- **`parse_snapshot`** — feed the exact JSON shapes documented in `MASSIVE.md` §4 (including a response missing a requested ticker), assert correct `{ticker: {...}}` output and that missing tickers are simply absent, not erroring.
- **`MassiveMarketDataSource`** — mock `httpx.AsyncClient.get` (via `httpx.MockTransport` or `respx`) to return canned 200/401/429/timeout responses; assert: successful poll updates cache, `401`/`429`/network errors are caught and logged without raising, cache retains last-known-good values across a failed poll, `prev_price` uses the previous poll's price (not `prevDay.c`) from the second poll onward.
- **Cache** — `update`, `update_many`, `get`, `get_all` behave as plain dict-backed storage; concurrent `asyncio` writers don't corrupt state (a fuzz test with `asyncio.gather` of many concurrent `update()` calls is sufficient given the lock).
- **`recompute_tracked_tickers`** — mock `db`, assert the union is computed and passed to `set_tracked_tickers` correctly (empty watchlist + one open position still tracks that ticker, etc.)

### 11.2 Interface conformance

Both implementations should pass a shared abstract test suite parameterized over `[MarketSimulator, MassiveMarketDataSource]`-style fixtures (with Massive's HTTP mocked) asserting the `MarketDataSource` contract itself: `start()` is idempotent-safe when called once, `stop()` cleans up without raising, `set_tracked_tickers()` before `start()` doesn't crash, ticks for untracked tickers never appear in the cache.

### 11.3 Fakeable for downstream tests

Endpoint-level tests (SSE, trade execution, watchlist) don't need either real implementation — a minimal fake satisfies the interface:

```python
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
```

### 11.4 E2E (`test/`, Playwright)

Runs with `MARKET_SIM_SEED` fixed and `LLM_MOCK=true` (PLAN.md §12) — Massive is never exercised in E2E since live market data is inherently non-deterministic. The simulator's seeded reproducibility is what makes assertions on specific price flash colors, heatmap colors, and P&L values reliable rather than flaky.

---

## 12. Non-Goals

- **No runtime source switching.** Simulator vs. Massive is fixed for the life of the process by `MASSIVE_API_KEY` at startup.
- **No multi-source blending.** Exactly one `MarketDataSource` is active; there's no "simulator for untracked tickers, Massive for tracked ones" hybrid.
- **No per-ticker historical persistence.** No `get_history(ticker)` method on the interface — sparklines/charts are purely client-side, accumulated from SSE since page load (PLAN.md §10).
- **No cross-process/multi-worker synchronization.** One `MarketDataSource` instance per process, matching PLAN.md §3's single-container architecture — no need to coordinate state across replicas.
- **No Massive plan-tier auto-detection.** The poll interval defaults to the free-tier-safe 15s; faster polling on a paid plan is an explicit env var override, not automatic detection.
