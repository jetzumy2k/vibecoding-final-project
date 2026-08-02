# FinAlly — AI Trading Workstation

FinAlly (Finance Ally) is a Bloomberg-terminal-style trading workstation: live-streaming market data, a simulated $10,000 portfolio, and an AI chat assistant that can analyze your positions and execute trades on your behalf.

This is the capstone project for an agentic AI coding course, built entirely by orchestrated coding agents. The full specification lives in [`planning/PLAN.md`](planning/PLAN.md) — read that first for architecture, API, schema, and design details.

## Stack

- **Frontend**: Next.js (TypeScript), static export
- **Backend**: FastAPI (Python, managed with `uv`)
- **Database**: SQLite, lazily initialized, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (`/api/stream/prices`)
- **AI**: LiteLLM → OpenRouter (Cerebras inference), structured outputs for trade/watchlist actions
- **Deployment**: single Docker container, single port (`8000`)

## Quick Start

```bash
cp .env.example .env   # add your OPENROUTER_API_KEY
./scripts/start_mac.sh # or scripts/start_windows.ps1 on Windows
```

Then open `http://localhost:8000`. No login required — you're dropped straight into the terminal with a default watchlist and $10,000 in virtual cash.

To stop:

```bash
./scripts/stop_mac.sh  # or scripts/stop_windows.ps1
```

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | Powers the AI chat assistant |
| `MASSIVE_API_KEY` | No | Real market data; omit to use the built-in simulator |
| `LLM_MOCK` | No | `true` for deterministic mock LLM responses (testing) |
| `MARKET_SIM_SEED` | No | Fixes the simulator's RNG for reproducible price sequences |

See `planning/PLAN.md` §5 for full details.

## Project Layout

```
frontend/    Next.js app (static export)
backend/     FastAPI app (uv project) — API, SSE, DB, market data, LLM integration
planning/    Shared documentation for the coding agents building this project
scripts/     Start/stop scripts (Docker)
test/        Playwright E2E tests
data/        SQLite database volume mount
```

## Testing

- Backend unit tests: `pytest` (within `backend/`)
- Frontend unit tests: within `frontend/`
- E2E tests: Playwright, run via `test/docker-compose.test.yml` against a deterministic build (`LLM_MOCK=true`, fixed `MARKET_SIM_SEED`)

## Market Simulator Demo

A standalone Rich terminal dashboard for watching the market simulator run live — all 10 default tickers, sparklines, color-coded direction arrows, and a notable-move event log:

```bash
# run from the repo root (backend must be importable as a package)
uv run --project backend --extra demo python -m backend.scripts.demo_terminal          # 60s, or Ctrl+C to stop early
uv run --project backend --extra demo python -m backend.scripts.demo_terminal --duration 30 --seed 42
```
