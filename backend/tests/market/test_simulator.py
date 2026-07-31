import asyncio

import pytest

import backend.market.simulator as simulator_module
from backend.market.cache import PriceCache
from backend.market.simulator import MarketSimulator
from backend.market.simulator_config import (
    DEFAULT_TICKERS,
    NEW_TICKER_MU,
    NEW_TICKER_SECTOR,
    NEW_TICKER_SIGMA,
    derive_seed_price,
)


class TestSetTrackedTickers:
    def test_default_ticker_uses_default_tickers_table(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"AAPL"})
        state = sim._state["AAPL"]
        price, mu, sigma, sector = DEFAULT_TICKERS["AAPL"]
        assert state.price == price
        assert state.prev_price == price
        assert state.mu == mu
        assert state.sigma == sigma
        assert state.sector == sector

    def test_unknown_ticker_uses_hash_derived_seed_price(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"PYPL"})
        state = sim._state["PYPL"]
        assert state.price == pytest.approx(derive_seed_price("PYPL"))
        assert state.mu == NEW_TICKER_MU
        assert state.sigma == NEW_TICKER_SIGMA
        assert state.sector == NEW_TICKER_SECTOR

    def test_removing_ticker_drops_its_state(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"AAPL", "GOOGL"})
        sim.set_tracked_tickers({"AAPL"})
        assert "GOOGL" not in sim._state
        assert "AAPL" in sim._state

    def test_re_adding_a_ticker_reseeds_its_state_from_scratch(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"AAPL"})
        sim._tick()  # perturb AAPL's price away from its seed
        assert sim._state["AAPL"].price != DEFAULT_TICKERS["AAPL"][0]

        sim.set_tracked_tickers(set())
        sim.set_tracked_tickers({"AAPL"})
        assert sim._state["AAPL"].price == DEFAULT_TICKERS["AAPL"][0]

    def test_existing_ticker_state_is_untouched_when_reasserted(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"AAPL"})
        sim._tick()
        perturbed_price = sim._state["AAPL"].price
        sim.set_tracked_tickers({"AAPL", "GOOGL"})
        assert sim._state["AAPL"].price == perturbed_price


class TestTick:
    def test_tick_with_no_tracked_tickers_does_not_raise(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim._tick()

    def test_tick_moves_prev_price_to_previous_price_and_updates_price(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"AAPL"})
        original_price = sim._state["AAPL"].price
        sim._tick()
        assert sim._state["AAPL"].prev_price == original_price

    def test_prices_stay_positive_after_many_ticks(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"AAPL", "TSLA", "NVDA"})
        for _ in range(200):
            sim._tick()
        assert all(state.price > 0 for state in sim._state.values())

    def test_price_floors_at_one_cent(self, monkeypatch):
        monkeypatch.setattr(simulator_module, "gbm_step", lambda *args, **kwargs: -50.0)
        sim = MarketSimulator(PriceCache(), seed=1)
        sim.set_tracked_tickers({"AAPL"})
        sim._tick()
        assert sim._state["AAPL"].price == 0.01


class TestDeterminism:
    def test_same_seed_produces_identical_price_sequences(self):
        tickers = {"AAPL", "GOOGL", "TSLA", "PYPL"}
        sim_a = MarketSimulator(PriceCache(), seed=123)
        sim_b = MarketSimulator(PriceCache(), seed=123)
        sim_a.set_tracked_tickers(tickers)
        sim_b.set_tracked_tickers(tickers)

        for _ in range(20):
            sim_a._tick()
            sim_b._tick()

        for ticker in tickers:
            assert sim_a._state[ticker].price == sim_b._state[ticker].price

    def test_different_seeds_produce_different_price_sequences(self):
        tickers = {"AAPL", "GOOGL", "TSLA"}
        sim_a = MarketSimulator(PriceCache(), seed=1)
        sim_b = MarketSimulator(PriceCache(), seed=2)
        sim_a.set_tracked_tickers(tickers)
        sim_b.set_tracked_tickers(tickers)

        for _ in range(20):
            sim_a._tick()
            sim_b._tick()

        prices_a = {t: sim_a._state[t].price for t in tickers}
        prices_b = {t: sim_b._state[t].price for t in tickers}
        assert prices_a != prices_b


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_only_writes_tracked_tickers_to_cache(self):
        cache = PriceCache()
        sim = MarketSimulator(cache, seed=1)
        sim.set_tracked_tickers({"AAPL", "GOOGL"})
        sim.set_tracked_tickers({"AAPL"})  # drops GOOGL state
        sim._tick()
        await sim._publish()
        all_ticks = await cache.get_all()
        assert set(all_ticks) == {"AAPL"}

    @pytest.mark.asyncio
    async def test_published_tick_direction_reflects_price_move(self):
        cache = PriceCache()
        sim = MarketSimulator(cache, seed=1)
        sim.set_tracked_tickers({"AAPL"})
        sim._tick()
        await sim._publish()
        tick = await cache.get("AAPL")
        state = sim._state["AAPL"]
        assert tick.price == state.price
        assert tick.prev_price == state.prev_price
        if state.price > state.prev_price:
            assert tick.direction == "up"
        elif state.price < state.prev_price:
            assert tick.direction == "down"
        else:
            assert tick.direction == "flat"


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_runs_background_loop_and_stop_cancels_it(self, monkeypatch):
        monkeypatch.setattr(simulator_module, "TICK_INTERVAL_SECONDS", 0.01)
        cache = PriceCache()
        sim = MarketSimulator(cache, seed=1)
        sim.set_tracked_tickers({"AAPL"})

        await sim.start()
        await asyncio.sleep(0.05)
        await sim.stop()

        tick = await cache.get("AAPL")
        assert tick is not None
        assert sim._task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(simulator_module, "TICK_INTERVAL_SECONDS", 0.01)
        sim = MarketSimulator(PriceCache(), seed=1)

        await sim.start()
        first_task = sim._task
        await sim.start()
        assert sim._task is first_task

        await sim.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_does_not_raise(self):
        sim = MarketSimulator(PriceCache(), seed=1)
        await sim.stop()
