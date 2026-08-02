"""Massive (Polygon.io rebrand) REST API client — batched multi-ticker
snapshot polling (planning/MASSIVE.md, planning/MARKET_DATA_DESIGN.md §7).
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

from backend.market.cache import PriceCache
from backend.market.interface import MarketDataSource, PriceTick

logger = logging.getLogger(__name__)

MASSIVE_BASE_URL = "https://api.massive.com"
FREE_TIER_POLL_SECONDS = 15.0
MAX_CONSECUTIVE_ERROR_LOGS = 1  # log the first error in a failure streak verbosely, then quiet down


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
        self._prev_prices: dict[str, float] = {}  # fallback prev_price when prevDay.c is stale/missing
        self._consecutive_errors = 0

    def set_tracked_tickers(self, tickers: set[str]) -> None:
        self._tracked = set(tickers)

    async def start(self) -> None:
        if self._task is None:
            self._client = httpx.AsyncClient(timeout=10.0)
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _run_loop(self) -> None:
        while True:
            if self._tracked:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Last-resort safety net: fail soft, log loud (MARKET_DATA_DESIGN.md §1).
                    # _poll_once already handles the known Massive failure modes below; this
                    # guards against anything unanticipated so the loop can never die silently.
                    logger.exception("Unexpected error during Massive poll for %s; will retry next interval", sorted(self._tracked))
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
        except ValueError as exc:
            # resp.json() raises this (json.JSONDecodeError) on a non-JSON body.
            self._log_error(f"Massive response was not valid JSON for {tickers}: {exc}")
            return

        try:
            parsed = parse_snapshot(payload)
        except (KeyError, TypeError) as exc:
            # A ticker item present but missing an expected field (e.g. no lastTrade
            # for a halted stock) — degrade like any other Massive failure mode.
            self._log_error(f"Massive snapshot payload malformed for {tickers}: {exc}")
            return

        self._consecutive_errors = 0
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
            logger.debug(message)  # avoid log-spamming on sustained outages
