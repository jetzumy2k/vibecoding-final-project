from backend.market.interface import PriceTick


class TestComputeDirection:
    def test_up_when_price_increased(self):
        assert PriceTick.compute_direction(101.0, 100.0) == "up"

    def test_down_when_price_decreased(self):
        assert PriceTick.compute_direction(99.0, 100.0) == "down"

    def test_flat_when_price_unchanged(self):
        assert PriceTick.compute_direction(100.0, 100.0) == "flat"


class TestPriceTick:
    def test_is_frozen(self):
        tick = PriceTick(
            ticker="AAPL",
            price=190.0,
            prev_price=189.0,
            timestamp="2026-07-29T14:32:01.123Z",
            direction="up",
        )
        try:
            tick.price = 200.0
            assert False, "expected FrozenInstanceError"
        except AttributeError:
            pass
