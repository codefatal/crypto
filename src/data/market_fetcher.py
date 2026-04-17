"""
Market Briefing Fetcher
───────────────────────
\uac70\uc2dc\uacbd\uc81c \uc9c0\uc218 / \uc8fc\ub3c4\uc8fc \uc2dc\uc138\ub97c yfinance\ub85c \uc218\uc9d1\ud569\ub2c8\ub2e4.

\ub300\uc0c1:
  \ubbf8 3\ub300 \uc9c0\uc218  : S&P 500 (^GSPC), NASDAQ (^IXIC), DOW (^DJI)
  \ud55c\uad6d       : KOSPI (^KS11)
  \uc8fc\ub3c4\uc8fc   : NVDA, AAPL, Samsung (005930.KS), BTC-USD, ETH-USD
  \uacf5\ud3ec\ud0d0\uc695 : alternative.me (NewsFetcher\uc640 \ub3d9\uc77c \uc18c\uc2a4)

yfinance history(period="5d", interval="1d")\ub97c \uc0ac\uc6a9\ud574 \uc8fc\ub9d0/\uc2e4\ud328 \ubaa8\ub450\uc5d0 \uc548\uc815\uc801\uc73c\ub85c \ub3d9\uc791\ud569\ub2c8\ub2e4.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


# ── \ub370\uc774\ud130 \ucee8\ud14c\uc774\ub108 ─────────────────────────────────────────────────────

@dataclass
class Quote:
    """
    \ub2e8\uc77c \uc790\uc0b0 \uc2dc\uc138 \uc2a4\ub0c5\uc0f7.
    change_pct : 1\uc77c \ub4f1\ub77d\ub960 (%, \uc608: +1.23 / -0.45)
    price      : \ucd5c\uc2e0\uac00 (None\uc774\uba74 \uc218\uc9d1 \uc2e4\ud328)
    """
    symbol: str
    name: str
    price: float | None
    change_pct: float | None


@dataclass
class MarketBriefing:
    """
    \uc2a4\ucf00\uc904\ub7ec\uac00 \uc218\uc9d1\ud558\ub294 \uc644\uc804\ud55c \uc2dc\uc7a5 \ube0c\ub9ac\ud551 \ud3ec\ud568 \ub370\uc774\ud130.
    indices    : \ubbf8\uad6d/\ud55c\uad6d \uc9c0\uc218 \ubaa9\ub85d
    leaders    : \uc8fc\ub3c4\uc8fc / \uc554\ud638\ud654\ud3d0 \ubaa9\ub85d
    fear_greed : \uacf5\ud3ec\ud0d0\uc695 \uc9c0\uc218 (0~100, None\uc774\uba74 \ubbf8\uc218\uc9d1)
    fear_label : "\uc5d0\ub514\uc158\uc758 \ud0d0\uc695" \ub4f1 \ud14d\uc2a4\ud2b8 \ub808\uc774\ube14
    """
    indices:    list[Quote] = field(default_factory=list)
    leaders:    list[Quote] = field(default_factory=list)
    fear_greed: int   | None = None
    fear_label: str   | None = None


# ── \uc2ec\ubcfc \uc815\uc758 ──────────────────────────────────────────────────────────

_INDEX_SYMBOLS: list[tuple[str, str]] = [
    ("^GSPC",     "S&P 500"),
    ("^IXIC",     "NASDAQ"),
    ("^DJI",      "DOW"),
    ("^KS11",     "KOSPI"),
]

_LEADER_SYMBOLS: list[tuple[str, str]] = [
    ("NVDA",       "NVDA"),
    ("AAPL",       "AAPL"),
    ("005930.KS",  "\uc0bc\uc131\uc804\uc790"),
    ("BTC-USD",    "BTC"),
    ("ETH-USD",    "ETH"),
]

_FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"


# ── \uc8fc\uc694 \ud568\uc218 ─────────────────────────────────────────────────────────

async def fetch_market_briefing() -> MarketBriefing:
    """
    \uac70\uc2dc\uacbd\uc81c \uc9c0\uc218 + \uc8fc\ub3c4\uc8fc + \uacf5\ud3ec\ud0d0\uc695\uc744 \ubcd1\ub82c\ub85c \uc218\uc9d1\ud569\ub2c8\ub2e4.
    \uc77c\ubd80 \uc2e4\ud328\ud574\ub3c4 \uc218\uc9d1\ub41c \ub370\uc774\ud130\ub97c \uadf8\ub300\ub85c \ubc18\ud658\ud569\ub2c8\ub2e4.
    """
    briefing = MarketBriefing()

    # yfinance \ubc31\uc5d4\ub4dc \ucda9\ub3cc \ubc29\uc9c0: \ubbf8\ub9ac \ub4e4\uc5ec\uc624\uae30
    try:
        import yfinance as yf  # noqa: PLC0415
    except ImportError:
        logger.warning("market_fetcher.yfinance_missing")
        return briefing

    indices_task  = asyncio.to_thread(_fetch_quotes_sync, yf, _INDEX_SYMBOLS)
    leaders_task  = asyncio.to_thread(_fetch_quotes_sync, yf, _LEADER_SYMBOLS)
    fg_task       = _fetch_fear_greed()

    indices, leaders, (fg_val, fg_label) = await asyncio.gather(
        indices_task, leaders_task, fg_task, return_exceptions=False
    )

    briefing.indices    = indices    if isinstance(indices,  list) else []
    briefing.leaders    = leaders    if isinstance(leaders,  list) else []
    briefing.fear_greed = fg_val
    briefing.fear_label = fg_label
    return briefing


async def fetch_btc_usd_price() -> float | None:
    """
    BTC-USD \ud604\uc7ac\uac00\ub97c \ubc18\ud658\ud569\ub2c8\ub2e4 (\uc5c5\ube44\ud2b8 \uacf5\uc9c0 \uac10\uc2dc\uc6a9).
    yfinance \ubbf8\uc0ac\uc6a9 \uc2dc None \ubc18\ud658.
    """
    try:
        import yfinance as yf  # noqa: PLC0415
        q = await asyncio.to_thread(_fetch_quotes_sync, yf, [("BTC-USD", "BTC")])
        return q[0].price if q else None
    except Exception as exc:
        logger.warning("market_fetcher.btc_price_failed", error=str(exc))
        return None


# ── \ub0b4\ubd80 \ud5ec\ud37c ─────────────────────────────────────────────────────────

def _fetch_quotes_sync(yf: Any, symbols: list[tuple[str, str]]) -> list[Quote]:
    """
    yfinance\ub97c \uc0ac\uc6a9\ud574 \uc2ec\ubcfc \ubaa9\ub85d\uc758 Quote\ub97c \uc77c\uad04 \uc218\uc9d1\ud569\ub2c8\ub2e4.
    history(period="5d")\ub97c \uc0ac\uc6a9\ud574 \uc8fc\ub9d0/\uc7a5\uc2e4\ub0a0 \uc548\uc815\uc131 \ud655\ubcf4.
    """
    quotes: list[Quote] = []
    for ticker_sym, display_name in symbols:
        try:
            hist = yf.Ticker(ticker_sym).history(period="5d", interval="1d")
            if hist is None or len(hist) < 2:
                quotes.append(Quote(symbol=ticker_sym, name=display_name, price=None, change_pct=None))
                continue
            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2])
            chg = (last_close - prev_close) / prev_close * 100.0 if prev_close else None
            quotes.append(Quote(
                symbol=ticker_sym,
                name=display_name,
                price=last_close,
                change_pct=round(chg, 2) if chg is not None else None,
            ))
        except Exception as exc:
            logger.warning("market_fetcher.quote_failed", symbol=ticker_sym, error=str(exc))
            quotes.append(Quote(symbol=ticker_sym, name=display_name, price=None, change_pct=None))
    return quotes


async def _fetch_fear_greed() -> tuple[int | None, str | None]:
    """alternative.me API\uc5d0\uc11c \uacf5\ud3ec\ud0d0\uc695 \uc9c0\uc218 \uc218\uc9d1."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(_FEAR_GREED_URL)
            resp.raise_for_status()
            data = resp.json()
            item = data["data"][0]
            return int(item["value"]), item.get("value_classification")
    except Exception as exc:
        logger.warning("market_fetcher.fear_greed_failed", error=str(exc))
        return None, None
