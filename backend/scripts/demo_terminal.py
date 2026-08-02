"""Rich terminal dashboard for the market data simulator.

Standalone demo, not used by the FastAPI app. Drives `MarketSimulator`
directly against all 10 default tickers and renders a live-updating
dashboard: price, change, direction arrow, and a rolling sparkline per
ticker, plus an event log of notable single-tick moves. Runs for a fixed
duration (default 60s) or until Ctrl+C, then prints a session summary
comparing final prices to seed prices.

Usage:
    uv run --extra demo python -m backend.scripts.demo_terminal
    uv run --extra demo python -m backend.scripts.demo_terminal --duration 30 --seed 42
"""

import argparse
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backend.market.cache import PriceCache
from backend.market.interface import PriceTick
from backend.market.simulator import TICK_INTERVAL_SECONDS, MarketSimulator
from backend.market.simulator_config import DEFAULT_TICKERS

DEFAULT_DURATION_SECONDS = 60.0
EVENT_MOVE_THRESHOLD = 0.015  # single-tick moves at/above this are logged as notable
SPARKLINE_WIDTH = 40
SPARKLINE_BLOCKS = "▁▂▃▄▅▆▇█"
EVENT_LOG_SIZE = 14

UP_STYLE = "bold green"
DOWN_STYLE = "bold red"
FLAT_STYLE = "dim white"


@dataclass
class TickerHistory:
    seed_price: float
    prices: list[float] = field(default_factory=list)
    last_price: float | None = None
    ticks_up: int = 0
    ticks_down: int = 0
    notable_events: int = 0


def sparkline(values: list[float]) -> str:
    window = values[-SPARKLINE_WIDTH:]
    if len(window) < 2:
        return ""
    lo, hi = min(window), max(window)
    if hi == lo:
        return SPARKLINE_BLOCKS[0] * len(window)
    span = hi - lo
    scale = len(SPARKLINE_BLOCKS) - 1
    return "".join(SPARKLINE_BLOCKS[min(int((v - lo) / span * scale), scale)] for v in window)


def direction_style(direction: str) -> str:
    return {"up": UP_STYLE, "down": DOWN_STYLE}.get(direction, FLAT_STYLE)


def direction_arrow(direction: str) -> Text:
    glyph = {"up": "▲", "down": "▼"}.get(direction, "●")
    return Text(glyph, style=direction_style(direction))


def build_table(histories: dict[str, TickerHistory], ticks: dict[str, PriceTick]) -> Table:
    table = Table(expand=True, border_style="grey37")
    table.add_column("Ticker", style="bold cyan")
    table.add_column("Price", justify="right")
    table.add_column("Chg", justify="right")
    table.add_column("Chg %", justify="right")
    table.add_column("Dir", justify="center")
    table.add_column("Sparkline (session)", justify="left")

    for ticker in sorted(histories):
        tick = ticks.get(ticker)
        if tick is None:
            table.add_row(ticker, "…", "", "", "", "")
            continue
        hist = histories[ticker]
        change = tick.price - tick.prev_price
        pct = (change / tick.prev_price * 100) if tick.prev_price else 0.0
        style = direction_style(tick.direction)
        table.add_row(
            ticker,
            f"${tick.price:,.2f}",
            Text(f"{change:+.2f}", style=style),
            Text(f"{pct:+.2f}%", style=style),
            direction_arrow(tick.direction),
            Text(sparkline(hist.prices), style="cyan"),
        )
    return table


def build_event_log(events: deque[str]) -> Panel:
    body = "\n".join(events) if events else "[dim]No notable moves yet…[/dim]"
    return Panel(body, title=f"Event Log (moves ≥ {EVENT_MOVE_THRESHOLD:.1%})", border_style="grey37")


def build_layout(histories: dict[str, TickerHistory], ticks: dict[str, PriceTick], events: deque[str], elapsed: float, duration: float) -> Layout:
    layout = Layout()
    remaining = max(duration - elapsed, 0.0)
    header = Text.from_markup(
        f"[bold]FinAlly Market Simulator[/bold] — live GBM feed   "
        f"[dim]elapsed[/dim] {elapsed:5.1f}s   [dim]remaining[/dim] {remaining:5.1f}s   [dim](Ctrl+C to stop early)[/dim]"
    )
    layout.split_column(
        Layout(Panel(header, border_style="grey37"), name="header", size=3),
        Layout(build_table(histories, ticks), name="table"),
        Layout(build_event_log(events), name="events", size=EVENT_LOG_SIZE + 2),
    )
    return layout


def record_ticks(histories: dict[str, TickerHistory], ticks: dict[str, PriceTick], events: deque[str], elapsed: float) -> None:
    for ticker, tick in ticks.items():
        hist = histories[ticker]
        hist.prices.append(tick.price)
        if tick.direction == "up":
            hist.ticks_up += 1
        elif tick.direction == "down":
            hist.ticks_down += 1

        if hist.last_price is not None and hist.last_price > 0:
            move = (tick.price - hist.last_price) / hist.last_price
            if abs(move) >= EVENT_MOVE_THRESHOLD:
                hist.notable_events += 1
                arrow = "▲" if move > 0 else "▼"
                style = "green" if move > 0 else "red"
                events.appendleft(
                    f"[dim]{elapsed:5.1f}s[/dim] [bold cyan]{ticker}[/bold cyan] "
                    f"[{style}]{arrow} {move:+.2%}[/{style}] -> ${tick.price:,.2f}"
                )
        hist.last_price = tick.price


def print_summary(console: Console, histories: dict[str, TickerHistory], elapsed: float, total_events: int, interrupted: bool) -> None:
    console.print()
    status = "stopped early (Ctrl+C)" if interrupted else "completed"
    console.print(f"[bold]Session {status}[/bold] after {elapsed:.1f}s\n")

    table = Table(title="Session Summary — Final vs. Seed", expand=True)
    table.add_column("Ticker", style="bold cyan")
    table.add_column("Seed", justify="right")
    table.add_column("Final", justify="right")
    table.add_column("Chg", justify="right")
    table.add_column("Chg %", justify="right")
    table.add_column("Ticks Up", justify="right")
    table.add_column("Ticks Down", justify="right")
    table.add_column("Events", justify="right")

    for ticker in sorted(histories):
        hist = histories[ticker]
        final_price = hist.prices[-1] if hist.prices else hist.seed_price
        change = final_price - hist.seed_price
        pct = (change / hist.seed_price * 100) if hist.seed_price else 0.0
        style = UP_STYLE if change > 0 else DOWN_STYLE if change < 0 else FLAT_STYLE
        table.add_row(
            ticker,
            f"${hist.seed_price:,.2f}",
            f"${final_price:,.2f}",
            Text(f"{change:+.2f}", style=style),
            Text(f"{pct:+.2f}%", style=style),
            str(hist.ticks_up),
            str(hist.ticks_down),
            str(hist.notable_events),
        )
    console.print(table)
    console.print(f"\n[dim]Total notable events across all tickers: {total_events}[/dim]")


async def run_demo(duration: float, seed: int | None) -> None:
    console = Console()
    cache = PriceCache()
    simulator = MarketSimulator(cache=cache, seed=seed)
    tracked = set(DEFAULT_TICKERS)
    simulator.set_tracked_tickers(tracked)
    histories = {ticker: TickerHistory(seed_price=DEFAULT_TICKERS[ticker][0]) for ticker in tracked}
    events: deque[str] = deque(maxlen=EVENT_LOG_SIZE)

    start = time.monotonic()
    interrupted = False
    await simulator.start()
    try:
        with Live(console=console, auto_refresh=False, screen=True) as live:
            while (elapsed := time.monotonic() - start) < duration:
                await asyncio.sleep(TICK_INTERVAL_SECONDS)
                elapsed = time.monotonic() - start
                ticks = await cache.get_all()
                record_ticks(histories, ticks, events, elapsed)
                live.update(build_layout(histories, ticks, events, elapsed, duration), refresh=True)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        await simulator.stop()

    elapsed = time.monotonic() - start
    total_events = sum(hist.notable_events for hist in histories.values())
    print_summary(console, histories, elapsed, total_events, interrupted)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rich terminal dashboard for the FinAlly market simulator.")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_SECONDS, help="Seconds to run (default: 60)")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for a reproducible price sequence")
    args = parser.parse_args()

    try:
        asyncio.run(run_demo(args.duration, args.seed))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
