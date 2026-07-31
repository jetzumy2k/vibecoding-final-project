import hashlib
import math
import random

import pytest

from backend.market.simulator import correlated_shocks, gbm_step, maybe_apply_event
from backend.market.simulator_config import derive_seed_price


class TestGbmStep:
    def test_zero_drift_zero_vol_zero_shock_leaves_price_unchanged(self):
        assert gbm_step(100.0, mu=0.0, sigma=0.0, dt_years=1.0, z=0.0) == 100.0

    def test_matches_the_documented_euler_maruyama_formula(self):
        price, mu, sigma, dt_years, z = 100.0, 0.10, 0.20, 1.0, 0.0
        expected = price * math.exp((mu - 0.5 * sigma**2) * dt_years + sigma * math.sqrt(dt_years) * z)
        assert gbm_step(price, mu, sigma, dt_years, z) == pytest.approx(expected)
        assert gbm_step(price, mu, sigma, dt_years, z) == pytest.approx(108.3287067674958)

    def test_matches_the_documented_formula_with_nonzero_shock(self):
        price, mu, sigma, dt_years, z = 190.0, 0.12, 0.28, 0.5 / (365 * 24 * 3600), 1.5
        expected = price * math.exp((mu - 0.5 * sigma**2) * dt_years + sigma * math.sqrt(dt_years) * z)
        assert gbm_step(price, mu, sigma, dt_years, z) == pytest.approx(expected)

    def test_positive_shock_increases_price_more_than_negative_shock(self):
        price, mu, sigma, dt_years = 100.0, 0.10, 0.20, 1.0
        up = gbm_step(price, mu, sigma, dt_years, z=1.0)
        down = gbm_step(price, mu, sigma, dt_years, z=-1.0)
        assert up > price > down

    def test_price_never_goes_negative_for_any_finite_shock(self):
        # GBM is multiplicative — exp() is always positive, so price stays positive
        # regardless of how negative the shock is.
        assert gbm_step(100.0, mu=0.1, sigma=0.5, dt_years=1.0, z=-10.0) > 0


class TestCorrelatedShocks:
    def test_same_seed_produces_deterministic_shocks(self):
        tickers_by_sector = {"tech": ["AAPL", "MSFT"], "finance": ["JPM"]}
        shocks_a = correlated_shocks(random.Random(42), tickers_by_sector, beta=0.6)
        shocks_b = correlated_shocks(random.Random(42), tickers_by_sector, beta=0.6)
        assert shocks_a == shocks_b

    def test_matches_manual_replication_of_the_draw_sequence(self):
        """correlated_shocks draws one Z per sector, then one Z per ticker, in
        sector-then-ticker order — replicate that exact draw sequence with an
        independently seeded rng and confirm the blended output matches.
        """
        tickers_by_sector = {"tech": ["AAPL", "MSFT"], "finance": ["JPM"]}
        beta = 0.6

        reference_rng = random.Random(7)
        expected: dict[str, float] = {}
        for sector, tickers in tickers_by_sector.items():
            z_sector = reference_rng.gauss(0, 1)
            for ticker in tickers:
                z_idio = reference_rng.gauss(0, 1)
                expected[ticker] = beta * z_sector + math.sqrt(1 - beta**2) * z_idio

        actual = correlated_shocks(random.Random(7), tickers_by_sector, beta=beta)

        assert actual == pytest.approx(expected)

    def test_single_ticker_sector_still_returns_a_shock(self):
        shocks = correlated_shocks(random.Random(1), {"tesla": ["TSLA"]}, beta=0.6)
        assert "TSLA" in shocks
        assert isinstance(shocks["TSLA"], float)

    def test_empty_sectors_returns_empty_shocks(self):
        assert correlated_shocks(random.Random(1), {}, beta=0.6) == {}


class _FakeRng:
    """Stub exposing just the rng surface maybe_apply_event uses, with fully
    controlled return values (avoids depending on real random.Random's exact
    output sequence, which is deterministic but not hand-computable here).
    """

    def __init__(self, random_values, uniform_value=0.03):
        self._random_values = list(random_values)
        self._uniform_value = uniform_value
        self.uniform_calls: list[tuple[float, float]] = []

    def random(self) -> float:
        return self._random_values.pop(0)

    def uniform(self, lo: float, hi: float) -> float:
        self.uniform_calls.append((lo, hi))
        return self._uniform_value


class TestMaybeApplyEvent:
    def test_no_event_when_probability_roll_misses(self):
        rng = _FakeRng(random_values=[0.99])  # > EVENT_PROBABILITY_PER_TICK
        assert maybe_apply_event(rng, 100.0) == 100.0
        assert rng.uniform_calls == []

    def test_upward_event_applies_positive_magnitude(self):
        # first random() triggers the event, second random() < 0.5 picks "up"
        rng = _FakeRng(random_values=[0.0, 0.1], uniform_value=0.03)
        assert maybe_apply_event(rng, 100.0) == pytest.approx(103.0)
        assert rng.uniform_calls == [(0.02, 0.05)]

    def test_downward_event_applies_negative_magnitude(self):
        # first random() triggers the event, second random() >= 0.5 picks "down"
        rng = _FakeRng(random_values=[0.0, 0.9], uniform_value=0.03)
        assert maybe_apply_event(rng, 100.0) == pytest.approx(97.0)


class TestDeriveSeedPrice:
    def test_matches_documented_sha256_bucket_formula(self):
        ticker = "PYPL"
        digest = hashlib.sha256(ticker.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 38_000
        expected = 20.00 + bucket / 100
        assert derive_seed_price(ticker) == pytest.approx(expected)

    def test_is_deterministic_across_calls(self):
        assert derive_seed_price("PYPL") == derive_seed_price("PYPL")

    def test_is_within_the_documented_price_range(self):
        for ticker in ["PYPL", "SHOP", "SQ", "COIN", "RBLX", "ZZZZ", "A"]:
            price = derive_seed_price(ticker)
            assert 20.00 <= price <= 399.99

    def test_different_tickers_generally_produce_different_prices(self):
        prices = {derive_seed_price(t) for t in ["PYPL", "SHOP", "SQ", "COIN", "RBLX"]}
        assert len(prices) == 5
