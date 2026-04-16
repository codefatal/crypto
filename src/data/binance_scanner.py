"""
Binance WebSocket Scanner
────────────────────────
• 바이낸스 전 USDT 종목을 실시간 감시
• WebSocket 멀티스트림 + REST 초기 히스토리 로드
• 캔들 데이터를 symbol → DataFrame 형태로 메모리 캐시

ACTIVE_EXCHANGE=binance 일 때 main.py가 이 모듈을 선택합니다.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

import pandas as pd
import structlog
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException
from tenacity import retry, stop_after_attempt, wait_exponential

from config import get_settings

logger = structlog.get_logger(__name__)

# 프로젝트 표준 OHLCV 컬럼
OHLCV_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


class BinanceScanner:
    """
    바이낸스 USDT 전 종목 WebSocket 스캐너.

    캔들이 완성될 때마다 on_signal(symbol, df)를 호출합니다.

    Args:
        on_signal: 완성 캔들 수신 시 호출할 async 콜백 (symbol, DataFrame)
        timeframe:  바이낸스 인터벌 (예: "15m", "1h"). None이면 settings.timeframe 사용.
        candle_limit: 메모리에 유지할 최대 캔들 수
    """

    def __init__(
        self,
        on_signal: Callable[[str, pd.DataFrame], Coroutine] | None = None,
        timeframe: str | None = None,
        candle_limit: int = 200,
    ) -> None:
        self._settings = get_settings()
        # Upbit 포맷("minute15")이 들어올 경우 Binance 포맷으로 변환
        raw_tf = timeframe or self._settings.timeframe
        self._timeframe = _to_binance_interval(raw_tf)
        self._candle_limit = candle_limit
        self._on_signal = on_signal

        self._client: AsyncClient | None = None
        self._bsm: BinanceSocketManager | None = None

        self._candles: dict[str, pd.DataFrame] = {}
        self._last_close: dict[str, int] = defaultdict(int)
        self._symbols: list[str] = []
        self._running = False

    # ── Public ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """스캐너 시작 (blocking). asyncio.create_task로 감싸서 사용."""
        self._client = await AsyncClient.create(
            api_key=self._settings.binance_api_key,
            api_secret=self._settings.binance_secret_key,
            testnet=self._settings.binance_testnet,
        )
        self._bsm = BinanceSocketManager(self._client)
        self._symbols = await self._fetch_symbols()
        logger.info("scanner.symbols_loaded", exchange="binance", count=len(self._symbols))

        await self._preload_history()

        self._running = True
        await self._stream_all()

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.close_connection()
        logger.info("scanner.stopped", exchange="binance")

    def get_candles(self, symbol: str) -> pd.DataFrame | None:
        return self._candles.get(symbol)

    def all_symbols(self) -> list[str]:
        return list(self._symbols)

    # ── Internal ──────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch_symbols(self) -> list[str]:
        """거래량 필터를 통과한 USDT 심볼 목록 반환"""
        info = await self._client.get_exchange_info()
        tickers = await self._client.get_ticker()
        volume_map = {
            t["symbol"]: float(t["quoteVolume"]) for t in tickers
        }
        symbols = [
            s["symbol"]
            for s in info["symbols"]
            if s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"
            and volume_map.get(s["symbol"], 0) >= self._settings.min_volume_usdt
        ]

        # 거래량 내림차순 정렬 후 상위 N개만 유지 (일일 토큰 예산 절약)
        max_syms = self._settings.max_symbols_per_candle
        symbols_sorted = sorted(symbols, key=lambda s: volume_map.get(s, 0), reverse=True)
        if max_syms > 0:
            symbols_sorted = symbols_sorted[:max_syms]
        return symbols_sorted

    async def _preload_history(self) -> None:
        """심볼별 초기 OHLCV 히스토리 병렬 로드"""
        semaphore = asyncio.Semaphore(20)

        async def _load(symbol: str) -> None:
            async with semaphore:
                try:
                    df = await self._fetch_klines(symbol)
                    self._candles[symbol] = df
                except Exception as exc:
                    logger.warning("preload.failed", symbol=symbol, error=str(exc))

        await asyncio.gather(*[_load(s) for s in self._symbols])
        logger.info("scanner.history_loaded", loaded=len(self._candles))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def _fetch_klines(self, symbol: str) -> pd.DataFrame:
        raw = await self._client.get_klines(
            symbol=symbol,
            interval=self._timeframe,
            limit=self._candle_limit,
        )
        return _klines_to_df(raw)

    async def _stream_all(self) -> None:
        """심볼을 25개 단위로 묶어 멀티스트림 구독"""
        chunk_size = 25
        chunks = [
            self._symbols[i:i + chunk_size]
            for i in range(0, len(self._symbols), chunk_size)
        ]
        await asyncio.gather(
            *[asyncio.create_task(self._run_chunk(c)) for c in chunks]
        )

    async def _run_chunk(self, symbols: list[str]) -> None:
        streams = [f"{s.lower()}@kline_{self._timeframe}" for s in symbols]
        async with self._bsm.multiplex_socket(streams) as ms:
            while self._running:
                try:
                    msg = await asyncio.wait_for(ms.recv(), timeout=30)
                    await self._handle_message(msg)
                except asyncio.TimeoutError:
                    logger.debug("scanner.stream_timeout")
                except Exception as exc:
                    logger.error("scanner.stream_error", error=str(exc))
                    await asyncio.sleep(1)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        if msg.get("e") == "error":
            logger.error("scanner.ws_error", msg=msg)
            return

        data = msg.get("data", msg)
        if data.get("e") != "kline":
            return

        kline = data["k"]
        symbol: str = data["s"]

        if not kline["x"]:
            return  # 미완성 캔들 무시

        close_time = int(kline["T"])
        if close_time <= self._last_close[symbol]:
            return  # 중복 방지
        self._last_close[symbol] = close_time

        new_row = _kline_msg_to_row(kline)
        self._update_buffer(symbol, new_row)

        if self._on_signal and symbol in self._candles:
            await self._on_signal(symbol, self._candles[symbol].copy())

    def _update_buffer(self, symbol: str, new_row: dict) -> None:
        row_df = pd.DataFrame([new_row])
        if symbol not in self._candles:
            self._candles[symbol] = row_df
        else:
            self._candles[symbol] = (
                pd.concat([self._candles[symbol], row_df], ignore_index=True)
                .tail(self._candle_limit)
                .reset_index(drop=True)
            )


# ── 헬퍼 ──────────────────────────────────────────────────────────────

# Upbit interval → Binance interval 변환 테이블
_UPBIT_TO_BINANCE: dict[str, str] = {
    "minute1": "1m", "minute3": "3m", "minute5": "5m",
    "minute10": "10m", "minute15": "15m", "minute30": "30m",
    "minute60": "1h", "minute240": "4h",
    "day": "1d", "week": "1w",
}


def _to_binance_interval(tf: str) -> str:
    """Upbit 포맷('minute15') 또는 Binance 포맷('15m') 모두 Binance 포맷으로 변환"""
    return _UPBIT_TO_BINANCE.get(tf, tf)


def _klines_to_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=OHLCV_COLS)
    for col in ["open", "high", "low", "close", "volume", "quote_volume",
                "taker_buy_base", "taker_buy_quote"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def _kline_msg_to_row(k: dict) -> dict:
    return {
        "open_time": pd.to_datetime(k["t"], unit="ms", utc=True),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "close_time": pd.to_datetime(k["T"], unit="ms", utc=True),
        "quote_volume": float(k["q"]),
        "trades": int(k["n"]),
        "taker_buy_base": float(k["V"]),
        "taker_buy_quote": float(k["Q"]),
        "ignore": "0",
    }
