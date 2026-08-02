import asyncio

import httpx
import pytest

from backend.market.cache import PriceCache
from backend.market.massive_client import (
    MassiveMarketDataSource,
    fetch_snapshot,
    parse_snapshot,
)

DEFAULT_TS_NS = 1_700_000_000_000_000_000  # ~2023-11-14T22:13:20Z


def make_snapshot_response(items: list[dict]) -> dict:
    return {"status": "OK", "count": len(items), "tickers": items}


def make_ticker_item(ticker: str, price: float, prev_close: float, ts_ns: int = DEFAULT_TS_NS) -> dict:
    return {
        "ticker": ticker,
        "lastTrade": {"p": price, "s": 100, "t": ts_ns},
        "prevDay": {"c": prev_close},
    }


class TestParseSnapshot:
    def test_parses_a_single_ticker(self):
        payload = make_snapshot_response([make_ticker_item("AAPL", 190.5, 189.0)])
        result = parse_snapshot(payload)
        assert result["AAPL"]["price"] == 190.5
        assert result["AAPL"]["prev_price"] == 189.0
        assert result["AAPL"]["timestamp"].startswith("2023-")

    def test_parses_multiple_tickers(self):
        payload = make_snapshot_response(
            [make_ticker_item("AAPL", 190.5, 189.0), make_ticker_item("GOOGL", 175.0, 174.0)]
        )
        result = parse_snapshot(payload)
        assert set(result) == {"AAPL", "GOOGL"}

    def test_ticker_missing_from_response_is_simply_absent(self):
        # caller requested AAPL+GOOGL but Massive only returned AAPL
        payload = make_snapshot_response([make_ticker_item("AAPL", 190.5, 189.0)])
        result = parse_snapshot(payload)
        assert "GOOGL" not in result

    def test_missing_prev_day_falls_back_to_current_price(self):
        item = {"ticker": "AAPL", "lastTrade": {"p": 190.5, "s": 100, "t": DEFAULT_TS_NS}}
        payload = make_snapshot_response([item])
        result = parse_snapshot(payload)
        assert result["AAPL"]["prev_price"] == 190.5

    def test_empty_tickers_list_returns_empty_dict(self):
        assert parse_snapshot({"status": "OK", "tickers": []}) == {}

    def test_missing_tickers_key_returns_empty_dict(self):
        assert parse_snapshot({"status": "OK"}) == {}


class TestFetchSnapshot:
    @pytest.mark.asyncio
    async def test_sends_comma_joined_tickers_and_api_key_as_query_params(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["tickers"] = request.url.params["tickers"]
            captured["apiKey"] = request.url.params["apiKey"]
            return httpx.Response(200, json=make_snapshot_response([make_ticker_item("AAPL", 190.5, 189.0)]))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        payload = await fetch_snapshot(client, ["AAPL", "GOOGL"], "test-key")
        await client.aclose()

        assert captured["tickers"] == "AAPL,GOOGL"
        assert captured["apiKey"] == "test-key"
        assert payload["tickers"][0]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_raises_on_http_error_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"status": "ERROR", "error": "Unauthorized"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_snapshot(client, ["AAPL"], "bad-key")
        await client.aclose()


class TestMassiveMarketDataSource:
    @pytest.mark.asyncio
    async def test_successful_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=make_snapshot_response([make_ticker_item("AAPL", 190.5, 189.0)]))

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()
        await source._client.aclose()

        tick = await cache.get("AAPL")
        assert tick.price == 190.5
        assert tick.prev_price == 189.0
        assert tick.direction == "up"

    @pytest.mark.asyncio
    async def test_prev_price_uses_previous_polls_price_not_prev_day_close(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        responses = [
            make_snapshot_response([make_ticker_item("AAPL", 190.0, 180.0)]),
            make_snapshot_response([make_ticker_item("AAPL", 191.0, 180.0)]),
        ]
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            resp = httpx.Response(200, json=responses[call_count["n"]])
            call_count["n"] += 1
            return resp

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()
        await source._poll_once()
        await source._client.aclose()

        tick = await cache.get("AAPL")
        # second poll's prev_price must be the FIRST poll's price (190.0), not prevDay.c (180.0)
        assert tick.prev_price == 190.0
        assert tick.price == 191.0

    @pytest.mark.asyncio
    async def test_401_error_is_caught_and_does_not_raise(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="bad-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"status": "ERROR"})

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()  # must not raise
        await source._client.aclose()

        assert await cache.get("AAPL") is None

    @pytest.mark.asyncio
    async def test_429_error_is_caught_and_cache_retains_last_known_good_values(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        responses = [
            httpx.Response(200, json=make_snapshot_response([make_ticker_item("AAPL", 190.0, 180.0)])),
            httpx.Response(429, json={"status": "ERROR"}),
        ]
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            resp = responses[call_count["n"]]
            call_count["n"] += 1
            return resp

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()
        await source._poll_once()  # 429 must not wipe the cache
        await source._client.aclose()

        tick = await cache.get("AAPL")
        assert tick.price == 190.0

    @pytest.mark.asyncio
    async def test_network_error_is_caught_and_does_not_raise(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()  # must not raise
        await source._client.aclose()
        assert await cache.get("AAPL") is None

    @pytest.mark.asyncio
    async def test_malformed_ticker_item_is_caught_and_does_not_raise(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        def handler(request: httpx.Request) -> httpx.Response:
            # e.g. a halted stock with no last trade — item present but missing "lastTrade"
            return httpx.Response(200, json=make_snapshot_response([{"ticker": "AAPL"}]))

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()  # must not raise
        await source._client.aclose()

        assert await cache.get("AAPL") is None

    @pytest.mark.asyncio
    async def test_malformed_payload_does_not_wipe_cache_or_reset_error_count(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        responses = [
            httpx.Response(200, json=make_snapshot_response([make_ticker_item("AAPL", 190.0, 180.0)])),
            httpx.Response(200, json=make_snapshot_response([{"ticker": "AAPL"}])),
        ]
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            resp = responses[call_count["n"]]
            call_count["n"] += 1
            return resp

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()
        await source._poll_once()  # malformed payload must not wipe the cache
        await source._client.aclose()

        tick = await cache.get("AAPL")
        assert tick.price == 190.0

    @pytest.mark.asyncio
    async def test_non_json_response_is_caught_and_does_not_raise(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL"})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()  # must not raise
        await source._client.aclose()

        assert await cache.get("AAPL") is None

    @pytest.mark.asyncio
    async def test_run_loop_survives_unexpected_exception_in_poll_once(self):
        # Simulates a bug in _poll_once that isn't one of its handled exception
        # types — the loop itself must still never die silently (MARKET_DATA_DESIGN.md §1).
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=0.01)
        source.set_tracked_tickers({"AAPL"})

        call_count = {"n": 0}

        async def failing_poll_once():
            call_count["n"] += 1
            raise RuntimeError("boom")

        source._poll_once = failing_poll_once
        source._task = asyncio.create_task(source._run_loop())
        await asyncio.sleep(0.05)
        source._task.cancel()
        try:
            await source._task
        except asyncio.CancelledError:
            pass

        assert call_count["n"] >= 2  # loop kept retrying instead of dying after the first error

    @pytest.mark.asyncio
    async def test_missing_ticker_in_response_does_not_crash(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=60)
        source.set_tracked_tickers({"AAPL", "GOOGL"})

        def handler(request: httpx.Request) -> httpx.Response:
            # Massive only returns AAPL even though GOOGL was also requested
            return httpx.Response(200, json=make_snapshot_response([make_ticker_item("AAPL", 190.0, 189.0)]))

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await source._poll_once()
        await source._client.aclose()

        assert await cache.get("AAPL") is not None
        assert await cache.get("GOOGL") is None

    @pytest.mark.asyncio
    async def test_poll_skipped_entirely_when_no_tickers_tracked(self):
        cache = PriceCache()
        source = MassiveMarketDataSource(cache, api_key="test-key", poll_interval=0.01)

        called = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, json=make_snapshot_response([]))

        source._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source._task = asyncio.create_task(source._run_loop())
        await asyncio.sleep(0.05)
        source._task.cancel()
        try:
            await source._task
        except asyncio.CancelledError:
            pass
        await source._client.aclose()

        assert called["count"] == 0

    def test_set_tracked_tickers_replaces_the_set(self):
        source = MassiveMarketDataSource(PriceCache(), api_key="test-key")
        source.set_tracked_tickers({"AAPL", "GOOGL"})
        source.set_tracked_tickers({"MSFT"})
        assert source._tracked == {"MSFT"}

    @pytest.mark.asyncio
    async def test_start_and_stop_lifecycle(self):
        # No tracked tickers, so the loop never actually reaches the network —
        # safe to exercise start()/stop() without mocking transport.
        source = MassiveMarketDataSource(PriceCache(), api_key="test-key", poll_interval=0.01)
        await source.start()
        await asyncio.sleep(0.03)
        await source.stop()
        assert source._task is None
        assert source._client is None
