import pytest

from backend.market.tracking import recompute_tracked_tickers


class FakeDB:
    def __init__(self, watchlist: list[str], positions: list[str]) -> None:
        self._watchlist = watchlist
        self._positions = positions

    async def get_watchlist_tickers(self, user_id: str) -> list[str]:
        return self._watchlist

    async def get_open_position_tickers(self, user_id: str) -> list[str]:
        return self._positions


class FakeSource:
    def __init__(self) -> None:
        self.tracked: set[str] | None = None

    def set_tracked_tickers(self, tickers: set[str]) -> None:
        self.tracked = tickers


@pytest.mark.asyncio
async def test_tracked_set_is_the_union_of_watchlist_and_positions():
    db = FakeDB(watchlist=["AAPL", "GOOGL"], positions=["GOOGL", "TSLA"])
    source = FakeSource()
    await recompute_tracked_tickers(db, source)
    assert source.tracked == {"AAPL", "GOOGL", "TSLA"}


@pytest.mark.asyncio
async def test_empty_watchlist_with_an_open_position_still_tracks_it():
    """A ticker removed from the watchlist keeps streaming while a position
    remains open (planning/PLAN.md §6, 'Tracked Ticker Set')."""
    db = FakeDB(watchlist=[], positions=["TSLA"])
    source = FakeSource()
    await recompute_tracked_tickers(db, source)
    assert source.tracked == {"TSLA"}


@pytest.mark.asyncio
async def test_empty_watchlist_and_no_positions_tracks_nothing():
    db = FakeDB(watchlist=[], positions=[])
    source = FakeSource()
    await recompute_tracked_tickers(db, source)
    assert source.tracked == set()


@pytest.mark.asyncio
async def test_defaults_to_the_default_user_id():
    captured = {}

    class RecordingDB:
        async def get_watchlist_tickers(self, user_id: str) -> list[str]:
            captured["watchlist_user"] = user_id
            return []

        async def get_open_position_tickers(self, user_id: str) -> list[str]:
            captured["positions_user"] = user_id
            return []

    await recompute_tracked_tickers(RecordingDB(), FakeSource())
    assert captured == {"watchlist_user": "default", "positions_user": "default"}


@pytest.mark.asyncio
async def test_passes_through_a_custom_user_id():
    captured = {}

    class RecordingDB:
        async def get_watchlist_tickers(self, user_id: str) -> list[str]:
            captured["watchlist_user"] = user_id
            return []

        async def get_open_position_tickers(self, user_id: str) -> list[str]:
            captured["positions_user"] = user_id
            return []

    await recompute_tracked_tickers(RecordingDB(), FakeSource(), user_id="someone-else")
    assert captured == {"watchlist_user": "someone-else", "positions_user": "someone-else"}
