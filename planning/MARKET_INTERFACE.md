# Market Data Interface Design

## Unified Python API for Live Stock Prices

This document specifies the abstract interface that both the market simulator (`MARKET_SIMULATOR.md`) and the Massive API client (`MASSIVE.md`) implement, so the rest of the backend (SSE streaming, trade execution, portfolio valuation) is completely agnostic to where prices come from. This directly implements PLAN.md §6 ("Two Implementations, One Interface").

## 1. Design Goals

- **Source-agnostic downstream code.** SSE streaming, trade execution, and portfolio valuation read from one shared cache and never import or branch on `Simulator` vs. `Massive`.
- **One background task, one writer.** Whichever source is active owns a single background loop that writes to a shared in-memory cache; nothing else writes to it.
- **Dynamic tracked-ticker set.** The set of tickers being priced is the watchlist ∪ open positions (PLAN.md §6), and it changes at runtime as the user adds/removes watchlist entries or opens/closes positions. Both implementations must support updating this set without a restart.
- **Env-var driven selection**, decided once at process startup: `MASSIVE_API_KEY` set and non-empty → Massive; otherwise → simulator. No runtime switching between the two.
- **Testable in isolation.** The interface must be mockable/fakeable for backend unit tests without touching either real implementation.

## 2. Module Layout

```
backend/
└── market/
    ├── __init__.py          # exports create_market_data_source(), PriceCache, PriceTick
    ├── interface.py          # MarketDataSource ABC, PriceTick dataclass
    ├── cache.py               # PriceCache (the shared in-memory store)
    ├── simulator.py          # MarketSimulator(MarketDataSource) — see MARKET_SIMULATOR.md
    ├── massive_client.py     # MassiveMarketDataSource(MarketDataSource) — see MASSIVE.md
    └── factory.py             # create_market_data_source(): env-var based selection
```

## 3. Core Types

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
    direction: str       # "up" | "down" | "flat" — derived from price vs. prev_price


class MarketDataSource(ABC):
    """Abstract interface implemented by both MarketSimulator and MassiveMarketDataSource.

    A source owns exactly one background polling/generation loop and writes
    every tick into the PriceCache passed to it at construction. It never
    reads from the cache itself — reads are the caller's job.
    """

    @abstractmethod
    async def start(self) -> None:
        """Begin the background loop. Must be safe to call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background loop and release any resources (HTTP clients, etc.)."""

    @abstractmethod
    def set_tracked_tickers(self, tickers: set[str]) -> None:
        """Replace the set of tickers this source generates/polls prices for.

        Called whenever the watchlist or open positions change. Implementations
        must pick up the new set on their next tick without restarting the loop.
        """
```

## 4. Shared Price Cache

The cache is a plain in-process object, not part of the `MarketDataSource` interface itself — both implementations are handed a reference to the same `PriceCache` instance at construction and write into it. SSE streaming and REST endpoints read from it independently.

```python
# backend/market/cache.py
import asyncio
from backend.market.interface import PriceTick


class PriceCache:
    """Single shared in-memory store of the latest tick per ticker.

    One writer (the active MarketDataSource's background loop), many readers
    (SSE connections, /api/portfolio, /api/watchlist). asyncio.Lock is
    sufficient since everything runs on a single event loop — no
    multi-process/multi-worker concerns given the single-container,
    single-user architecture (PLAN.md §3, §7).
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

This directly satisfies PLAN.md §6 ("Shared Price Cache" — holds latest price, previous price, and timestamp; SSE streams read from it).

## 5. Factory: Env-Var-Based Selection

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

Called once at FastAPI startup:

```python
# backend/main.py (excerpt)
price_cache = PriceCache()
market_data_source = create_market_data_source(price_cache)

@app.on_event("startup")
async def startup() -> None:
    await market_data_source.start()

@app.on_event("shutdown")
async def shutdown() -> None:
    await market_data_source.stop()
```

`MARKET_SIM_SEED` is read only by the simulator branch — it's meaningless when Massive is active (PLAN.md §5 already specifies this: "ignored when MASSIVE_API_KEY is set").

## 6. Tracked Ticker Set Management

Per PLAN.md §6, the tracked set is **watchlist ∪ open positions**, recomputed whenever either changes. This logic lives outside `MarketDataSource` (it's a query against the `watchlist` and `positions` tables), and gets pushed into whichever source is active via `set_tracked_tickers`:

```python
# backend/market/tracking.py
async def recompute_tracked_tickers(db, market_data_source: MarketDataSource) -> None:
    watchlist_tickers = await db.get_watchlist_tickers(user_id="default")
    position_tickers = await db.get_open_position_tickers(user_id="default")
    market_data_source.set_tracked_tickers(set(watchlist_tickers) | set(position_tickers))
```

Call sites: after `POST /api/watchlist`, after `DELETE /api/watchlist/{ticker}`, and after every trade execution in `POST /api/portfolio/trade` (a trade can open a new position or close one out to zero, both of which change the set).

Each implementation handles a tracked-set update differently, but both conform to the same contract (set is fully replaced, next tick reflects it):

- **Simulator**: adds GBM state for newly-tracked tickers (deriving a seed price via the hash-based scheme if not a predefined default, per PLAN.md §6 and `MARKET_SIMULATOR.md` §4), and drops state for no-longer-tracked tickers.
- **Massive**: simply changes the `tickers` CSV param used in its next poll request. No per-ticker state to manage — the API is stateless from FinAlly's point of view.

## 7. Consumption by SSE Streaming

The SSE endpoint never touches `MarketDataSource` — it only reads the cache, decoupling the streaming cadence from the generation/polling cadence (relevant since the simulator ticks every ~500ms but Massive on the free tier only refreshes every ~15s; the SSE endpoint can still push at a consistent client-facing interval, simply re-sending the same values until the cache updates):

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

## 8. Trade Execution Reads

`POST /api/portfolio/trade` reads the fill price directly from the cache at request time — never a client-supplied price (PLAN.md §8):

```python
async def execute_trade(ticker: str, side: str, quantity: float, cache: PriceCache, db) -> dict:
    tick = await cache.get(ticker)
    if tick is None:
        raise TickerNotTrackedError(ticker)
    fill_price = tick.price
    # ... validate cash/shares, write positions + trades rows using fill_price ...
```

## 9. Testing Strategy

- `MarketDataSource` is trivially fakeable in backend unit tests: a `FakeMarketDataSource` that writes fixed ticks into a real `PriceCache` on `start()`, with no background loop at all.
- Simulator determinism (`MARKET_SIM_SEED`) and `LLM_MOCK` together make the full E2E suite reproducible per PLAN.md §12 — the interface boundary here is what makes that possible, since E2E tests only ever assert against cache/API output, never against simulator or Massive internals directly.
- Massive-specific tests (response parsing, error handling) mock `httpx` responses against the JSON shapes documented in `MASSIVE.md` §4-§5 — no live network calls in CI.

## 10. Non-Goals

- No per-ticker historical price persistence server-side (PLAN.md §10 — sparklines and the main chart are built client-side from SSE ticks accumulated since page load). The interface does not need a `get_history(ticker)` method.
- No runtime source switching. The choice of simulator vs. Massive is fixed for the life of the process by the environment at startup.
- No multi-source blending (e.g., simulator for untracked tickers, Massive for tracked ones). Exactly one `MarketDataSource` implementation is active at a time.
