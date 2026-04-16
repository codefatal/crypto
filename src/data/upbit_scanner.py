"""
Upbit WebSocket Scanner
────────────────────────
• 업비트 KRW 마켓 전 종목을 실시간 감시
• pyupbit WebSocketManager(ticker) → asyncio Queue 브릿지
• OHLCV는 캔들 경계 시각에 REST로 일괄 수집 (Upbit는 kline WS 미지원)

흐름:
  1. KRW 마켓 심볼 목록 수집 + 거래대금 필터
  2. 초기 OHLCV 히스토리 병렬 로드 (pyupbit.get_ohlcv)
  3. ticker WebSocket → asyncio Queue 브릿지 (스레드 → 이벤트루프)
  4. 캔들 타이머 루프: 캔들 경계 시각에 OHLCV 일괄 갱신 + on_signal 호출
"""
from __future__ import annotations

import asyncio
import queue as _queue
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Coroutine

import pandas as pd
import pyupbit
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from config import get_settings

logger = structlog.get_logger(__name__)

# pyupbit interval 값과 해당 초(seconds) 매핑
# (업비트 지원 interval: minute1/3/5/10/15/30/60/240, day, week, month)
_INTERVAL_SECONDS: dict[str, int] = {
    "minute1": 60,
    "minute3": 180,
    "minute5": 300,
    "minute10": 600,
    "minute15": 900,
    "minute30": 1800,
    "minute60": 3600,
    "minute240": 14400,
    "day": 86400,
    "week": 604800,
}

# Binance 스타일 표기 → pyupbit interval 변환 (하위 호환)
_BINANCE_TO_UPBIT: dict[str, str] = {
    "1m": "minute1", "3m": "minute3", "5m": "minute5",
    "10m": "minute10", "15m": "minute15", "30m": "minute30",
    "1h": "minute60", "4h": "minute240",
    "1d": "day", "1w": "week",
}

# 업비트 OHLCV DataFrame을 프로젝트 표준 컬럼으로 매핑
_UPBIT_RENAME = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "value": "quote_volume",  # KRW 거래대금
}


class UpbitScanner:
    """
    업비트 KRW 마켓 전 종목 스캐너.

    캔들 경계 시각에 on_signal(symbol, df)를 호출합니다.
    df는 완성된 캔들만 포함 (현재 형성 중인 마지막 캔들 제외).

    Args:
        on_signal: 캔들 완성 시 호출할 async 콜백 (symbol, DataFrame)
        timeframe:  pyupbit interval 문자열 또는 Binance 스타일 (예: "minute15", "15m")
        candle_limit: 메모리에 유지할 최대 캔들 수
    """

    def __init__(
        self,
        on_signal: Callable[[str, pd.DataFrame], Coroutine] | None = None,
        timeframe: str | None = None,
        candle_limit: int = 200,
    ) -> None:
        self._settings = get_settings()

        raw_tf = timeframe or self._settings.timeframe
        self._interval = _BINANCE_TO_UPBIT.get(raw_tf, raw_tf)
        if self._interval not in _INTERVAL_SECONDS:
            raise ValueError(
                f"지원하지 않는 타임프레임: '{raw_tf}'. "
                f"지원값: {list(_INTERVAL_SECONDS)}"
            )
        self._candle_sec = _INTERVAL_SECONDS[self._interval]
        self._candle_limit = candle_limit
        self._on_signal = on_signal

        # symbol → DataFrame (완성된 캔들 rolling buffer)
        self._candles: dict[str, pd.DataFrame] = {}
        # symbol → live 현재가 (ticker WS에서 갱신)
        self._live_price: dict[str, float] = {}
        # symbol → 24h KRW 거래대금 (ticker WS에서 갱신)
        self._volume_krw: dict[str, float] = {}

        self._symbols: list[str] = []
        self._ws: pyupbit.WebSocketManager | None = None
        self._running = False

    # ── Public ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """스캐너 시작 (blocking). asyncio.create_task로 감싸서 사용."""
        self._symbols = await self._fetch_symbols()
        logger.info("scanner.symbols_loaded", count=len(self._symbols))

        await self._preload_history()

        self._ws = pyupbit.WebSocketManager("ticker", self._symbols)
        self._running = True
        logger.info("scanner.started", interval=self._interval)

        await asyncio.gather(
            self._ws_consumer_loop(),
            self._candle_timer_loop(),
        )

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.terminate()
        logger.info("scanner.stopped")

    def get_candles(self, symbol: str) -> pd.DataFrame | None:
        return self._candles.get(symbol)

    def get_live_price(self, symbol: str) -> float | None:
        return self._live_price.get(symbol)

    def all_symbols(self) -> list[str]:
        return list(self._symbols)

    # ── 심볼 수집 ─────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
    async def _fetch_symbols(self) -> list[str]:
        """
        KRW 마켓 심볼 중 24h KRW 거래대금이 min_volume_krw 이상인 것만 반환.
        pyupbit REST는 동기이므로 to_thread로 호출.
        """
        tickers_raw: list[dict] = await asyncio.to_thread(
            pyupbit.get_tickers, fiat="KRW"
        )
        # get_tickers returns list of str like ["KRW-BTC", ...]
        all_symbols: list[str] = tickers_raw if isinstance(tickers_raw[0], str) else [
            t["market"] for t in tickers_raw
        ]

        # 거래대금 필터: pyupbit.get_ohlcv_from는 느리므로 초기엔 전체 포함
        # (ticker WS 수신 후 _volume_krw가 채워지면 동적 필터 가능)
        # 프로덕션에서는 pyupbit.get_tickers(fiat="KRW", details=True)로 24h 거래대금 조회 가능
        logger.info("scanner.all_krw_symbols", count=len(all_symbols))
        return sorted(all_symbols)

    # ── 히스토리 초기 로드 ────────────────────────────────────────────

    async def _preload_history(self) -> None:
        """
        심볼별 OHLCV 초기 로드.
        업비트 REST API: 초당 10 req 제한 → Semaphore(8)로 안전하게 병렬화.
        """
        semaphore = asyncio.Semaphore(8)

        async def _load(symbol: str) -> None:
            async with semaphore:
                try:
                    df = await self._fetch_ohlcv(symbol)
                    if df is not None:
                        self._candles[symbol] = df
                except Exception as exc:
                    logger.warning("preload.failed", symbol=symbol, error=str(exc))

        await asyncio.gather(*[_load(s) for s in self._symbols])
        logger.info(
            "scanner.history_loaded",
            loaded=len(self._candles),
            total=len(self._symbols),
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def _fetch_ohlcv(self, symbol: str) -> pd.DataFrame | None:
        """
        pyupbit.get_ohlcv 동기 호출을 to_thread로 래핑.
        완성된 캔들만 반환 (마지막 행 = 현재 형성 중 → 제외).
        """
        raw: pd.DataFrame | None = await asyncio.to_thread(
            pyupbit.get_ohlcv,
            symbol,
            interval=self._interval,
            count=self._candle_limit + 1,  # 마지막 1개(미완성)를 버릴 여유
        )
        if raw is None or raw.empty:
            return None

        df = _normalize_ohlcv(raw, symbol)
        # 마지막 행(현재 형성 중인 캔들) 제거
        return df.iloc[:-1].reset_index(drop=True)

    # ── WebSocket 소비 루프 ───────────────────────────────────────────

    async def _ws_consumer_loop(self) -> None:
        """
        pyupbit WebSocketManager(mp.Process 기반)의 내부 Queue를
        asyncio.to_thread로 비동기 bridge하여 소비합니다.

        pyupbit 0.2.33: 내부 큐는 name-mangled private 속성(_WebSocketManager__q).
        WebSocketManager.get()은 타임아웃 파라미터 없음 → 내부 큐 직접 접근.
        timeout=2초: 이벤트 루프를 2초 이상 점유하지 않도록 제어.
        """
        logger.info("scanner.ws_consumer.started")

        # WebSocket 프로세스 시작 (alive 플래그 + start)
        if not self._ws.alive:  # type: ignore[union-attr]
            self._ws.alive = True  # type: ignore[union-attr]
            self._ws.start()  # type: ignore[union-attr]

        # name-mangled private multiprocessing.Queue 직접 접근
        _mp_q = self._ws._WebSocketManager__q  # type: ignore[union-attr]

        while self._running:
            try:
                msg = await asyncio.to_thread(_mp_q.get, True, 2.0)
                if isinstance(msg, dict):
                    self._handle_ticker(msg)
            except _queue.Empty:
                pass  # timeout 정상 처리
            except Exception as exc:
                logger.warning("scanner.ws_read_error", error=str(exc))
                await asyncio.sleep(1)

        logger.info("scanner.ws_consumer.stopped")

    def _handle_ticker(self, msg: dict) -> None:
        """ticker 메시지에서 현재가 + 24h KRW 거래대금 갱신"""
        code: str = msg.get("code", "")
        price: float = float(msg.get("trade_price", 0))
        vol_krw: float = float(msg.get("acc_trade_price_24h", 0))

        if code and price > 0:
            self._live_price[code] = price
        if code and vol_krw > 0:
            self._volume_krw[code] = vol_krw

    # ── 캔들 타이머 루프 ──────────────────────────────────────────────

    async def _candle_timer_loop(self) -> None:
        """
        캔들 경계 시각(e.g., 매 15분 정각 + 2초 버퍼)에 깨어나
        모든 심볼의 OHLCV를 갱신하고 on_signal 콜백을 호출합니다.

        +2초 버퍼: 업비트 서버가 새 캔들 데이터를 확정하는 시간 여유.
        """
        logger.info("scanner.candle_timer.started", interval=self._interval)
        while self._running:
            wait_sec = self._seconds_until_next_candle() + 2.0
            logger.debug("scanner.next_candle_in", seconds=round(wait_sec, 1))
            await asyncio.sleep(wait_sec)

            if not self._running:
                break

            logger.info("scanner.candle_boundary", interval=self._interval)
            await self._emit_all_candles()

        logger.info("scanner.candle_timer.stopped")

    def _seconds_until_next_candle(self) -> float:
        """현재 시각 기준 다음 캔들 경계까지 남은 초"""
        now = time.time()
        next_boundary = (now // self._candle_sec + 1) * self._candle_sec
        return max(next_boundary - now, 0.0)

    async def _emit_all_candles(self) -> None:
        """
        전 심볼 OHLCV 일괄 갱신.
        거래대금 필터: ticker WS로 수집한 24h KRW 거래대금이
        min_volume_krw 미만인 심볼은 건너뜁니다.
        """
        min_vol = self._settings.min_volume_krw
        active = [
            s for s in self._symbols
            if self._volume_krw.get(s, min_vol) >= min_vol  # 아직 미수신 심볼은 포함
        ]

        # 거래대금 내림차순으로 정렬 후 상위 N개만 분석 (일일 토큰 예산 절약)
        max_syms = self._settings.max_symbols_per_candle
        if max_syms > 0:
            active = sorted(
                active,
                key=lambda s: self._volume_krw.get(s, 0),
                reverse=True,
            )[:max_syms]

        semaphore = asyncio.Semaphore(8)

        async def _fetch_and_emit(symbol: str) -> None:
            async with semaphore:
                try:
                    df = await self._fetch_ohlcv(symbol)
                    if df is None or df.empty:
                        return
                    self._candles[symbol] = df
                    if self._on_signal:
                        await self._on_signal(symbol, df.copy())
                except Exception as exc:
                    logger.warning(
                        "scanner.emit_failed", symbol=symbol, error=str(exc)
                    )

        await asyncio.gather(*[_fetch_and_emit(s) for s in active])
        logger.info("scanner.candles_emitted", count=len(active))


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────

def _normalize_ohlcv(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    pyupbit get_ohlcv 결과를 프로젝트 표준 OHLCV 포맷으로 변환.

    pyupbit 컬럼: open, high, low, close, volume, value
    표준 컬럼:    open_time, open, high, low, close, volume, quote_volume, symbol
    """
    df = raw.copy()
    df.index.name = "open_time"
    df = df.reset_index()

    # open_time: pyupbit는 KST naive → UTC aware로 변환
    df["open_time"] = pd.to_datetime(df["open_time"]).dt.tz_localize(
        "Asia/Seoul"
    ).dt.tz_convert("UTC")

    # 컬럼 정규화
    df = df.rename(columns={"value": "quote_volume"})

    # 숫자 타입 보장
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        if col in df.columns:
            df[col] = df[col].astype(float)

    df["symbol"] = symbol
    return df[["open_time", "open", "high", "low", "close", "volume", "quote_volume", "symbol"]]
