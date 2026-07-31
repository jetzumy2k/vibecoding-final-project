import random

from backend.market.cache import PriceCache
from backend.market.factory import create_market_data_source
from backend.market.massive_client import MassiveMarketDataSource
from backend.market.simulator import MarketSimulator


class TestCreateMarketDataSource:
    def test_returns_massive_source_when_api_key_set(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "abc123")
        source = create_market_data_source(PriceCache())
        assert isinstance(source, MassiveMarketDataSource)
        assert source._api_key == "abc123"

    def test_returns_simulator_when_api_key_unset(self, monkeypatch):
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        source = create_market_data_source(PriceCache())
        assert isinstance(source, MarketSimulator)

    def test_returns_simulator_when_api_key_is_blank(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "   ")
        source = create_market_data_source(PriceCache())
        assert isinstance(source, MarketSimulator)

    def test_simulator_uses_market_sim_seed_when_set(self, monkeypatch):
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        monkeypatch.setenv("MARKET_SIM_SEED", "42")
        source = create_market_data_source(PriceCache())
        assert isinstance(source, MarketSimulator)
        assert source._rng.getstate() == random.Random(42).getstate()

    def test_simulator_is_unseeded_when_market_sim_seed_unset(self, monkeypatch):
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        monkeypatch.delenv("MARKET_SIM_SEED", raising=False)
        source = create_market_data_source(PriceCache())
        assert isinstance(source, MarketSimulator)

    def test_market_sim_seed_is_ignored_when_massive_api_key_set(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "abc123")
        monkeypatch.setenv("MARKET_SIM_SEED", "42")
        source = create_market_data_source(PriceCache())
        assert isinstance(source, MassiveMarketDataSource)
