import asyncio

import pytest

from backend.market.cache import PriceCache
from backend.market.interface import PriceTick


def make_tick(ticker: str, price: float, prev_price: float | None = None) -> PriceTick:
    return PriceTick(
        ticker=ticker,
        price=price,
        prev_price=prev_price if prev_price is not None else price,
        timestamp="2026-07-29T14:32:01.123Z",
        direction=PriceTick.compute_direction(price, prev_price if prev_price is not None else price),
    )


@pytest.mark.asyncio
async def test_get_missing_ticker_returns_none():
    cache = PriceCache()
    assert await cache.get("AAPL") is None


@pytest.mark.asyncio
async def test_update_then_get_returns_latest_tick():
    cache = PriceCache()
    tick = make_tick("AAPL", 190.0)
    await cache.update(tick)
    assert await cache.get("AAPL") == tick


@pytest.mark.asyncio
async def test_update_overwrites_previous_tick():
    cache = PriceCache()
    await cache.update(make_tick("AAPL", 190.0))
    second = make_tick("AAPL", 191.0, prev_price=190.0)
    await cache.update(second)
    assert await cache.get("AAPL") == second


@pytest.mark.asyncio
async def test_update_many_writes_all_ticks():
    cache = PriceCache()
    ticks = [make_tick("AAPL", 190.0), make_tick("GOOGL", 175.0)]
    await cache.update_many(ticks)
    all_ticks = await cache.get_all()
    assert all_ticks == {"AAPL": ticks[0], "GOOGL": ticks[1]}


@pytest.mark.asyncio
async def test_get_all_returns_copy_not_live_reference():
    cache = PriceCache()
    await cache.update(make_tick("AAPL", 190.0))
    snapshot = await cache.get_all()
    snapshot["AAPL"] = "mutated"
    assert (await cache.get("AAPL")).price == 190.0


@pytest.mark.asyncio
async def test_concurrent_writers_do_not_corrupt_state():
    cache = PriceCache()
    tickers = [f"T{i}" for i in range(50)]

    async def writer(ticker: str) -> None:
        for price in range(1, 21):
            await cache.update(make_tick(ticker, float(price)))

    await asyncio.gather(*(writer(t) for t in tickers))

    all_ticks = await cache.get_all()
    assert set(all_ticks) == set(tickers)
    assert all(tick.price == 20.0 for tick in all_ticks.values())
