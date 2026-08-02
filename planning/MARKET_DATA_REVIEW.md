# Market Data Backend — Review

Reviewed `backend/market/` against `planning/MARKET_DATA_DESIGN.md` (the consolidated, implementation-ready spec) and its source docs (`MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE.md`). Read every module (`interface.py`, `cache.py`, `tracking.py`, `simulator_config.py`, `simulator.py`, `massive_client.py`, `factory.py`, `__init__.py`) line-by-line against the doc's code listings, ran the full test suite, and independently exercised the simulator and the Massive client's error paths (including one not covered by the existing tests) with ad hoc scripts.

## Test Results

```
uv run pytest -v
83 passed in 0.72s
```

All 83 tests pass, with no warnings or skips. Coverage spans: `PriceCache` concurrency/copy semantics, `PriceTick` direction/frozen-ness, `recompute_tracked_tickers` union logic, the factory's env-var selection matrix, the simulator's pure functions (`gbm_step`, `correlated_shocks`, `maybe_apply_event`, `derive_seed_price`) with exact numeric assertions, simulator lifecycle/determinism/tracked-set mutation, `MassiveMarketDataSource` polling/error handling, `parse_snapshot`/`fetch_snapshot`, a shared conformance suite parameterized over both real implementations, and the `FakeMarketDataSource` test double. This is a genuinely thorough suite — better than the "sufficient" bar set by §11 of the design doc.

Manually re-ran the simulator end-to-end (real `start()`/`stop()`, 4 tickers, 2 seconds of wall-clock ticking) and confirmed it produces sane, distinct prices with correct flash directions.

## Findings

### P1 — A single malformed/unexpected Massive response permanently and silently kills price polling

**`backend/market/massive_client.py:91-103`** — `_poll_once()`'s exception handling only covers HTTP-transport-level failures:

```python
try:
    payload = await fetch_snapshot(self._client, tickers, self._api_key)
except httpx.HTTPStatusError as exc:
    ...
except httpx.HTTPError as exc:
    ...

self._consecutive_errors = 0
parsed = parse_snapshot(payload)   # <-- unguarded
```

Nothing catches errors from `resp.json()` (a `json.JSONDecodeError`, not an `httpx.HTTPError`) or from `parse_snapshot()` itself, which does unguarded dict/key access (`item["ticker"]`, `item["lastTrade"]["p"]`, `item["lastTrade"]["t"]`). Any ticker item that's present in `tickers[]` but missing an expected field — e.g. a halted stock with no last trade, or any deviation from the exact shape in `MASSIVE.md` §4, which that doc itself flags as not fully verified against a live response (`MASSIVE.md` §7: "exact error-body JSON shape ... was not verified against a live response during this research") — raises an uncaught exception that propagates out of `_poll_once()` and out of `_run_loop()`, terminating the background `asyncio.Task` for good.

I reproduced this directly:

```python
# 200 OK, but the ticker item is missing "lastTrade" (e.g. no trades yet today)
{"status": "OK", "tickers": [{"ticker": "AAPL"}]}
```
```
task done? True
exception: KeyError('lastTrade')
```

Because `start()` fires the loop with a bare `asyncio.create_task(self._run_loop())` and nothing ever awaits or inspects that task during normal operation, this failure is **completely silent** — no log line, no crash, no exception surfaced anywhere. The price cache simply freezes at its last values forever, and the watchlist/portfolio UI would show stale prices indefinitely with zero indication anything is wrong, until the process is restarted. If `stop()` is eventually called, it will itself raise `KeyError` (the `except asyncio.CancelledError` clause doesn't match an already-completed task's original exception), so even graceful shutdown surfaces the wrong error at the wrong time.

This directly contradicts the design doc's explicit contract (`MARKET_DATA_DESIGN.md` §1): *"Fail soft, log loud. A transient Massive error ... must not crash the process or blank out the cache — it should log and retain the last known good prices until the next successful poll."* The current code satisfies this only for the subset of failures that happen to be `httpx.HTTPError`/`HTTPStatusError`; anything else is worse than a crash because it's undetectable.

**Fix**: widen `_poll_once()`'s except clause (or wrap the `parse_snapshot`/JSON-decode step separately) to catch `Exception` broadly — consistent with the existing `_log_error`/`_consecutive_errors` de-dup machinery already built for exactly this purpose — so a malformed payload degrades to "log once, retry next interval" like every other Massive failure mode, instead of permanently killing the loop.

**Related test-coverage gap**: no test in `test_massive_client.py` constructs a `200 OK` response with a missing/malformed field on an otherwise-valid ticker entry — every error-path test (401, 429, network error) exercises `httpx`-level failures only. This is exactly the blind spot that let the bug through; a test for this case would have caught it.

### P2 — `.env` is tracked in git with no root `.gitignore`

Not a market-data-backend defect, but directly relevant to it: this backend introduces `MASSIVE_API_KEY` as a second secret (alongside `OPENROUTER_API_KEY`) that's meant to live in `.env`. `git ls-files` shows `.env` is currently tracked (0 bytes today, but tracked), and there is no root-level `.gitignore` anywhere in the repo — only `backend/.gitignore` (which only covers `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`). `PLAN.md` §5/§11 assumes `.env` is gitignored with a committed `.env.example`; neither is true right now, so the next `git add` touching `.env` after either key is filled in would commit both API keys. This was already flagged as P1 in `planning/REVIEW.md` before any implementation existed and still hasn't been addressed.

### P3 — `backend/uv.lock` is not committed

`PLAN.md` §3 lists "reproducible lockfile" as the specific rationale for choosing `uv`. Running `uv sync` in a clean checkout generates `backend/uv.lock` from scratch (it wasn't present before this review), so different machines/CI runs could currently resolve different dependency versions. Low risk today given the shallow, pinned-lower-bound dependency list (`httpx>=0.27`, pytest, pytest-asyncio), but worth committing once the dependency surface grows (LiteLLM, FastAPI, uvicorn, etc. are all still to come per `PLAN.md` §9).

## Design Conformance

Verified against `MARKET_DATA_DESIGN.md`'s code listings section by section — implementation matches near-verbatim, including subtleties that would be easy to drop:

| Area | Status |
|---|---|
| `PriceTick`/`MarketDataSource` interface (§3) | Matches exactly, including `compute_direction` as a static helper |
| `PriceCache` (§4) | Matches exactly, including lock-protected `get_all()` returning a copy |
| `recompute_tracked_tickers` (§5) | Matches, uses a `Protocol` for the db dependency (cleaner than the doc's untyped `db` param) |
| Seed prices / GBM / correlated shocks / event jumps (§6) | Matches formulas exactly; `sorted()` iteration preserved everywhere it matters for RNG-sequence determinism |
| Own `random.Random` instance, never global `random` (§6.5) | Correct |
| `derive_seed_price` uses `sha256`, not builtin `hash()` (§6.1) | Correct — this is the detail most likely to get "simplified" away and it wasn't |
| Massive fetch/parse (§7.1) | Matches, including the ns-timestamp division and `prevDay.c` fallback |
| `_prev_prices` overriding `prevDay.c` after the first poll (§7.4) | Correctly implemented and correctly tested (`test_prev_price_uses_previous_polls_price_not_prev_day_close`) |
| Error handling table (§7.3) | Handles the *listed* cases (401/403, 404-via-missing-ticker, 429, network) correctly — see P1 above for the gap outside that table |
| Factory env-var selection (§8) | Matches exactly, including `MARKET_SIM_SEED` being inert when `MASSIVE_API_KEY` is set |
| `start()`/`stop()` idempotency | Both implementations guard `start()` with `if self._task is None` and null out `_task`/`_client` in `stop()` — stricter than the doc's listing, which didn't guard `start()` at all |
| Module boundary (§2: "nothing outside `market/` imports `simulator.py`/`massive_client.py` directly") | Not yet verifiable — no code outside `backend/market/` exists yet (no `main.py`, no `api/`), so this boundary hasn't been tested against a real consumer |

No FastAPI/DB integration exists yet (`backend/main.py`, `backend/api/`, `backend/db/` are all absent) — expected, since this task was scoped to the market data backend only. `§9` of the design doc (FastAPI wiring) is therefore unimplemented by definition, not a defect.

## Recommendation

The module is well-built and faithfully implements the design doc, with a strong, thoughtfully-written test suite. Fix the P1 finding before this is exercised against live Massive traffic — it's a real "silently stops working forever" failure mode, not a hypothetical one, and it's a small, contained fix (widen one except clause, add one test). The P2/P3 items are pre-existing repo-hygiene gaps worth closing before `OPENROUTER_API_KEY`/`MASSIVE_API_KEY` are ever actually filled in, but they don't block this specific piece of work.
