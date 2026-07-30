# Massive API Reference

## Research Notes for FinAlly's Market Data Integration

## 1. What Is Massive?

**Massive is a rebrand of Polygon.io**, effective October 30, 2025. It is *not* a new API — endpoint paths, request/response schemas, and API keys are unchanged from Polygon.io. Existing Polygon.io code, keys, and docs continue to work.

- New base URL: `https://api.massive.com`
- Legacy base URL (still fully functional in parallel): `https://api.polygon.io`
- Docs live at `https://massive.com/docs/...`; old `polygon.io/docs/...` links 301-redirect there.
- Old Python package `polygon-api-client` (`from polygon import RESTClient`) still works; new package is `massive` (`from massive import RESTClient`).

**Use `api.massive.com` as the base URL in FinAlly** — it's the forward-looking host, and `api.polygon.io` is expected to sunset at some unannounced future date.

> Note: "Polygon" the blockchain network (Polygon/MATIC) is an unrelated company. Don't confuse their docs with Massive/Polygon.io's market data docs when searching.

---

## 2. Authentication

Two interchangeable methods, both work on any endpoint:

```bash
# Query parameter (simplest — recommended for FinAlly)
curl "https://api.massive.com/v2/aggs/ticker/AAPL/prev?apiKey=YOUR_API_KEY"

# Header
curl -H "Authorization: Bearer YOUR_API_KEY" \
  "https://api.massive.com/v2/aggs/ticker/AAPL/prev"
```

Store the key in `MASSIVE_API_KEY` (already specified in PLAN.md §5). Use the query-param form in FinAlly's client — it keeps a plain `httpx`/`requests` implementation simple with no header plumbing.

---

## 3. Rate Limits

| Tier | Price | Rate limit | Data recency |
|---|---|---|---|
| Basic (free) | $0 | **5 requests/minute** | End-of-day / 15-min delayed, 2yr history |
| Starter | $29/mo | Unlimited calls | 15-min delayed, 5yr history |
| Developer | $79/mo | Unlimited calls | 15-min delayed, 10yr history |
| Advanced | $199/mo | Unlimited calls | Real-time, 20+yr history |

**Correction to a common assumption**: paid tiers don't grant a faster-but-still-limited requests/minute quota. They grant **unlimited call volume**; what changes across paid tiers is *data recency* (delayed vs. real-time) and *history depth*, not throughput. The only hard requests/minute ceiling in the whole pricing table is the free tier's 5/min.

**Implication for FinAlly**: on the free tier, poll at ~every 12-15 seconds to stay safely under 5 calls/min (matches PLAN.md §6's stated default). On any paid tier, polling frequency is limited only by how fresh the underlying data actually is (real-time only on Advanced+) — polling faster than the data updates just burns calls for no benefit.

---

## 4. Batched Multi-Ticker Snapshot (live prices for the whole watchlist in one call)

This is the endpoint FinAlly's market data poller should use every tick — one request returns current data for the entire tracked ticker set (watchlist ∪ open positions per PLAN.md §6), so call volume doesn't scale with watchlist size.

```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT&apiKey=YOUR_API_KEY
```

- `tickers`: comma-separated, case-sensitive (e.g. `AAPL,TSLA,GOOGL`). Omitting it returns the *entire* market (10,000+ tickers) — always pass an explicit list.
- `include_otc`: bool, default `false`.

Response (abbreviated, real field names):

```json
{
  "count": 1,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": -0.124,
      "todaysChangePerc": -0.601,
      "updated": 1605192894630916600,
      "day":      { "o": 190.64, "h": 191.20, "l": 189.50, "c": 190.50, "v": 37216000, "vw": 190.62 },
      "prevDay":  { "o": 190.79, "h": 191.00, "l": 189.50, "c": 190.63, "v": 29273800, "vw": 190.69 },
      "min":      { "o": 190.50, "h": 190.55, "l": 190.48, "c": 190.50, "v": 5000, "vw": 190.51, "t": 1684428600000, "n": 1, "av": 37216 },
      "lastTrade": { "p": 190.50, "s": 100, "t": 1605192894630916600, "x": 4, "i": "71675577320245" },
      "lastQuote": { "p": 190.48, "P": 190.52, "s": 1, "S": 2, "t": 1605192959994246100 }
    }
  ]
}
```

Field abbreviation cheat sheet:

| Field | Meaning |
|---|---|
| `o,h,l,c,v,vw,n` | open, high, low, close, volume, volume-weighted avg price, number of trades |
| `t` (in `min`, `lastTrade`, `lastQuote`) | timestamp — **nanoseconds** epoch in trade/quote objects, **milliseconds** in bar objects (`day`/`prevDay`/`min` use ms; verify against the field you're reading) |
| `lastTrade.p` / `s` | last trade price / size |
| `lastQuote.p` / `P` | bid price / ask price |
| `lastQuote.s` / `S` | bid size / ask size |

**Mapping to FinAlly's price cache** (PLAN.md §6): use `lastTrade.p` as the current price, `prevDay.c` (or the previously cached price from the prior poll) as the "previous price" for computing tick direction/flash color, and `updated` (or `lastTrade.t`) as the tick timestamp.

There is also a newer `v3/snapshot` unified endpoint (`ticker.any_of=...`, up to 250 tickers, cross-asset-class, tolerates unknown tickers per-item instead of failing the whole batch). It's more general but has a different response shape (`results` array, `error`/`message` per bad ticker). **Not needed for FinAlly's ~10-20 ticker scale** — the `v2` snapshot above is simpler and sufficient. Documented here in case the tracked ticker set ever needs partial-failure tolerance for unknown tickers.

---

## 5. Daily Aggregate (End-of-Day) Bars

For per-ticker historical daily bars (not currently in FinAlly's live-streaming path, but useful if historical chart backfill is ever added):

```
GET https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-06-13?adjusted=true&sort=asc&limit=5000&apiKey=YOUR_API_KEY
```

```json
{
  "ticker": "AAPL",
  "status": "OK",
  "adjusted": true,
  "results": [
    { "t": 1577941200000, "o": 74.06, "h": 75.15, "l": 73.80, "c": 75.09, "v": 135647456, "vw": 74.61, "n": 1 }
  ]
}
```

Previous day's single bar (cheaper than a range query when you only need yesterday's close):

```
GET https://api.massive.com/v2/aggs/ticker/AAPL/prev?apiKey=YOUR_API_KEY
```

Whole-market single-day summary (all tickers, one date — note the **uppercase `"T"`** for ticker symbol, distinct from lowercase `"t"` for timestamp; a common parsing gotcha):

```
GET https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2024-06-13?adjusted=true&apiKey=YOUR_API_KEY
```

```json
{
  "adjusted": true, "queryCount": 3, "resultsCount": 3, "status": "OK",
  "results": [
    { "T": "AAPL", "o": 74.06, "h": 75.15, "l": 73.80, "c": 75.09, "v": 135647456, "vw": 74.61, "t": 1577941200000, "n": 1 }
  ]
}
```

---

## 6. Python Usage

### Recommended: plain HTTP client (`httpx`), not the official SDK

An official SDK exists (`pip install massive` → `from massive import RESTClient`, or the legacy `pip install polygon-api-client` → `from polygon import RESTClient`). However, its README only clearly documents single-ticker convenience methods (`list_aggs`, `list_trades`, `get_last_quote`) — the batch snapshot method name (§4) could not be confirmed with confidence during research.

For FinAlly, call the REST endpoint directly:

```python
import httpx

MASSIVE_BASE_URL = "https://api.massive.com"

async def fetch_snapshot(tickers: list[str], api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{MASSIVE_BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(tickers), "apiKey": api_key},
        )
        resp.raise_for_status()
        return resp.json()
```

This keeps the implementation transparent, trivially mockable for tests, and avoids depending on an unverified SDK method name. It also matches how the simulator implementation (see `MARKET_SIMULATOR.md`) will be structured, so both sides of the `MarketDataSource` interface (see `MARKET_INTERFACE.md`) stay simple and symmetrical.

### Parsing the batch response into FinAlly's cache shape

```python
from datetime import datetime, timezone

def parse_snapshot(payload: dict) -> dict[str, dict]:
    """Returns {ticker: {"price": float, "prev_price": float, "timestamp": str}}"""
    out = {}
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
```

---

## 7. Errors to Handle

| Status | Meaning | Handling |
|---|---|---|
| 200 with `"status": "OK"` | Success | Normal path |
| 200 with `"status": "DELAYED"` | Success, but data is delayed per your plan tier | Treat as normal; it's still the freshest data your key is entitled to |
| 401 | Unauthorized — missing/invalid `apiKey` | Fail fast, log clearly (almost always a misconfigured `MASSIVE_API_KEY`) |
| 403 / `NOT_AUTHORIZED` | Valid key, but plan doesn't entitle you to this endpoint or data class | Fail fast, log clearly |
| 404 | Bad ticker or endpoint path | For the `v2` batch snapshot, an unknown ticker is dropped from `tickers[]` rather than 404ing the whole request — the client's tracked-ticker set may come back with fewer entries than requested; the poller should tolerate that (log a warning, don't crash) |
| 429 | Rate limit exceeded | Back off; on free tier this means the poll interval is too aggressive — widen it |

The exact error-body JSON shape (`{"status": "ERROR", "error": "...", "message": "..."}` vs. others) was not verified against a live response during this research — verify empirically against a real API call before writing strict parsing logic around it. Handle by HTTP status code first, and treat the JSON body as advisory/logging-only.

---

## Sources

- https://massive.com/blog/polygon-is-now-massive
- https://massive.com/knowledge-base/article/does-polygon-have-an-endpoint-that-returns-the-latest-trades-quotes-and-aggregates-in-one-request
- https://massive.com/knowledge-base/article/what-is-the-request-limit-for-polygons-restful-apis
- https://massive.com/docs/rest/stocks/snapshots/unified-snapshot
- https://massive.com/docs/rest/stocks/aggregates/custom-bars
- https://massive.com/docs/rest/stocks/aggregates/daily-market-summary
- https://massive.com/docs/rest/stocks/aggregates/previous-day-bar
- https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary
- https://massive.com/pricing
- https://github.com/massive-com/client-python
- https://pypi.org/project/polygon-api-client/
