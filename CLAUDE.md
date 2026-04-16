# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- **Python**: 3.14 (`.venv/Scripts/python.exe`)
- **Package install**: `pip install -r requirements.txt --only-binary pandas,numpy` (pandas/numpy require pre-built wheels on Python 3.14)
- **Run**: `python main.py` from project root (reads `.env` automatically via pydantic-settings)

## Common Commands

```bash
# Run tests
.venv/Scripts/python.exe -m pytest tests/ -q

# Run a single test file
.venv/Scripts/python.exe -m pytest tests/test_indicator.py -v

# Run a single test
.venv/Scripts/python.exe -m pytest tests/test_analyzer.py::TestExtractJson::test_plain_json -v

# Lint
.venv/Scripts/python.exe -m ruff check src/ main.py config/

# Auto-fix lint
.venv/Scripts/python.exe -m ruff check --fix src/ main.py config/

# Format
.venv/Scripts/python.exe -m black src/ main.py config/
```

## Architecture

### Data Flow (one candle cycle)

```
WebSocket ticker (pyupbit mp.Process)
    │
    ▼
UpbitScanner._ws_consumer_loop()   — live price / volume update
    │
    ▼  (at candle boundary)
UpbitScanner._emit_all_candles()   — REST OHLCV fetch, Semaphore(8)
    │  on_signal(symbol, df)
    ▼
main.py._process_symbol()
    ├─ BakktaIndicator.compute()   — score 0-100; skip if score < 55
    ├─ asyncio.to_thread(db.log_indicator)
    ├─ news_cache.for_coin(coin)   — coin-filtered NewsContext
    ├─ AIAnalyzer.analyze()        — Groq API, Semaphore(4), 429 sleep
    └─ if HIGH confidence → notifier.send_signal() + trader.execute()
```

### Exchange Toggle

`ACTIVE_EXCHANGE=upbit|binance` in `.env` controls which modules load. Both scanners and traders are lazy-imported inside factory functions in `main.py` (`_build_scanner`, `_build_trader`) — the inactive exchange's packages are **never imported**, avoiding `websockets==12.0` vs `python-binance` conflicts.

### Module Responsibilities

| Module | Role |
|---|---|
| `config/settings.py` | Single `Settings` (pydantic-settings). Always import via `get_settings()` — never read `os.environ` directly. `lru_cache(1)` means settings are frozen at startup. |
| `src/indicator/bakkta.py` | Pure numpy/pandas, no I/O. `BakktaResult.is_tradeable(min_score=55)` gates all AI calls. |
| `src/ai/analyzer.py` | `AIAnalyzer` wraps Groq (OpenAI-compat). Three-layer JSON safety: regex extraction → jsonschema → Pydantic. Falls back to `neutral_fallback()` after `AI_MAX_RETRIES` exhausted. |
| `src/ai/schemas.py` | `TradeSignal` (Pydantic), `TRADE_SIGNAL_JSON_SCHEMA` (injected into system prompt), `neutral_fallback()`. |
| `src/data/news_fetcher.py` | `NewsFetcher.fetch_recent()` returns `NewsContext` (naver + RSS + fear/greed). `NewsContext.for_coin(coin)` filters naver items per-symbol; global headlines and fear/greed are shared. |
| `src/execution/logger.py` | `ReasoningLogger` — SQLAlchemy sync ORM, called via `asyncio.to_thread`. Uses `NullPool` for SQLite (prevents FlushError from concurrent threads). |
| `src/execution/notifier.py` | Discord webhook embed + Telegram Bot API. All public methods use `asyncio.gather(..., return_exceptions=True)` — one failed channel never blocks the other. |
| `src/execution/trader.py` | `UpbitTrader` + `OrderResult` dataclass (shared by BinanceTrader). |

### Key Constraints

- **`websockets==12.0` is pinned** — pyupbit 0.2.33 requires the legacy `websockets.legacy` API removed in v13.
- **pyupbit WebSocketManager** is an `mp.Process` (not a thread). Its internal queue is name-mangled `_WebSocketManager__q`; accessed directly in `_ws_consumer_loop` with `asyncio.to_thread(_mp_q.get, True, 2.0)`.
- **Groq free tier**: 12,000 TPM. `AIAnalyzer._semaphore = Semaphore(4)` limits concurrent calls. On 429, the retry-after seconds are parsed from the error message and slept.
- **SQLite + asyncio**: NullPool gives each `to_thread` call its own connection. Do not switch back to StaticPool.
- **pydantic-settings `.env` parsing**: inline comments on value lines break float/int parsing. Keep comments on separate lines.

### VSCode Debugging (F5)

Uses **attach mode** (not launch) to avoid a Python 3.14 + debugpy `wait_for_ready_to_run()` hang. F5 triggers a `preLaunchTask` that starts `main.py` with `DEBUGPY_PORT=5678` (or 5679/5680/5681 for alternate timeframes); the process calls `debugpy.listen()` and waits, then VSCode attaches.

Available F5 configurations (Run & Debug dropdown): `15분봉` (default, port 5678), `1분봉` (5679), `5분봉` (5680), `1시간봉` (5681).

### Database Schema

Three SQLAlchemy tables in `src/execution/logger.py`:
- `ai_decisions` — every `AIDecision` logged (including fallbacks)
- `indicator_snapshots` — `BakktaResult` at candle boundary
- `trade_signals` — placed orders with fill/PnL updates

Default: `sqlite:///autocrypto.db` (auto-created). Switch to PostgreSQL by changing `DATABASE_URL` in `.env` — no code changes needed.
