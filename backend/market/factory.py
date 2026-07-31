"""Env-var driven selection of the active MarketDataSource.

Selection happens once, at process startup — there is no runtime switching
between simulator and Massive (planning/MARKET_DATA_DESIGN.md §8).
"""

import os

from backend.market.cache import PriceCache
from backend.market.interface import MarketDataSource
from backend.market.massive_client import MassiveMarketDataSource
from backend.market.simulator import MarketSimulator


def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveMarketDataSource(cache=cache, api_key=api_key)

    seed = os.environ.get("MARKET_SIM_SEED")
    return MarketSimulator(cache=cache, seed=int(seed) if seed else None)
