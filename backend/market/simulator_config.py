"""Per-ticker seed prices, drift/volatility parameters, and sector buckets
for the market simulator (planning/MARKET_SIMULATOR.md §3-§4).
"""

import hashlib

# ticker -> (seed_price, mu [annual drift], sigma [annual vol], sector)
DEFAULT_TICKERS: dict[str, tuple[float, float, float, str]] = {
    "AAPL": (190.00, 0.12, 0.28, "tech"),
    "GOOGL": (175.00, 0.10, 0.30, "tech"),
    "MSFT": (420.00, 0.11, 0.26, "tech"),
    "AMZN": (185.00, 0.13, 0.32, "tech"),
    "TSLA": (250.00, 0.05, 0.55, "tesla"),  # own sector: idiosyncratic, high-vol
    "NVDA": (130.00, 0.20, 0.45, "tech"),
    "META": (560.00, 0.14, 0.34, "tech"),
    "JPM": (210.00, 0.08, 0.22, "finance"),
    "V": (310.00, 0.09, 0.20, "finance"),
    "NFLX": (680.00, 0.10, 0.30, "tech"),
}

NEW_TICKER_MU = 0.08
NEW_TICKER_SIGMA = 0.30
NEW_TICKER_SECTOR = "general"


def derive_seed_price(ticker: str) -> float:
    """Deterministic $20.00-$399.99 seed price for tickers outside DEFAULT_TICKERS.

    Uses sha256, not Python's builtin hash() — hash() is salted per-process
    (PYTHONHASHSEED) and would break reproducibility across runs/tests.
    """
    digest = hashlib.sha256(ticker.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 38_000  # 0..37999
    return 20.00 + bucket / 100
