"""
AutoCrypto — 메인 진입점
─────────────────────────
팩토리 패턴으로 거래소를 동적 선택합니다.

  .env: ACTIVE_EXCHANGE=upbit   → UpbitScanner  + UpbitTrader
  .env: ACTIVE_EXCHANGE=binance → BinanceScanner + BinanceTrader

파이프라인:
  Scanner ──캔들완성──▶ BakktaIndicator ──▶ AIAnalyzer
                                               │
                           ┌────────────────────┘
                           ▼
               Trader ──▶ Notifier ──▶ ReasoningLogger
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import structlog
from structlog.dev import ConsoleRenderer

from config import get_settings
from src.ai.analyzer import AIAnalyzer
from src.data.news_fetcher import NewsContext, NewsFetcher
from src.execution.logger import ReasoningLogger
from src.execution.notifier import Notifier
from src.indicator.bakkta import BakktaIndicator

# ── 로깅 설정 ─────────────────────────────────────────────────────────
settings = get_settings()

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        (
            ConsoleRenderer()
            if settings.log_level == "DEBUG"
            else structlog.processors.JSONRenderer()
        ),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(__import__("logging"), settings.log_level, 20)
    ),
)

logger = structlog.get_logger(__name__)


# ── 거래소 팩토리 ─────────────────────────────────────────────────────

@runtime_checkable
class Scanner(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def all_symbols(self) -> list[str]: ...


@runtime_checkable
class Trader(Protocol):
    async def init(self) -> None: ...
    async def close(self) -> None: ...
    async def execute(self, decision) -> None: ...


def _build_scanner(on_signal):
    """ACTIVE_EXCHANGE 설정에 따라 적절한 스캐너 인스턴스를 생성합니다."""
    exchange = settings.active_exchange
    if exchange == "upbit":
        from src.data.upbit_scanner import UpbitScanner
        logger.info("factory.scanner", selected="UpbitScanner")
        return UpbitScanner(on_signal=on_signal)
    elif exchange == "binance":
        from src.data.binance_scanner import BinanceScanner
        logger.info("factory.scanner", selected="BinanceScanner")
        return BinanceScanner(on_signal=on_signal)
    raise ValueError(
        f"지원하지 않는 거래소: {exchange!r}. "
        "ACTIVE_EXCHANGE를 'upbit' 또는 'binance'로 설정하세요."
    )


def _build_trader():
    """ACTIVE_EXCHANGE 설정에 따라 적절한 트레이더 인스턴스를 생성합니다."""
    exchange = settings.active_exchange
    if exchange == "upbit":
        from src.execution.trader import UpbitTrader
        logger.info("factory.trader", selected="UpbitTrader")
        return UpbitTrader()
    elif exchange == "binance":
        from src.execution.binance_trader import BinanceTrader
        logger.info("factory.trader", selected="BinanceTrader")
        return BinanceTrader()
    raise ValueError(
        f"지원하지 않는 거래소: {exchange!r}. "
        "ACTIVE_EXCHANGE를 'upbit' 또는 'binance'로 설정하세요."
    )


def _extract_coin(symbol: str) -> str:
    """
    심볼에서 코인 티커만 추출합니다.
    - Upbit   : "KRW-BTC"  → "BTC"
    - Binance : "BTCUSDT"  → "BTC"
    """
    if symbol.startswith("KRW-"):
        return symbol[4:]            # Upbit
    return symbol.replace("USDT", "")  # Binance (USDT 마켓)


def _exchange_display() -> dict[str, str]:
    """시작 알림 메시지용 거래소별 표시 정보"""
    if settings.active_exchange == "upbit":
        return {
            "거래소": "업비트 (KRW 마켓)",
            "최소거래대금": f"{settings.min_volume_krw:,.0f} KRW",
            "최대포지션": f"{settings.max_position_krw:,.0f} KRW",
        }
    return {
        "거래소": "바이낸스 (USDT 마켓)",
        "최소거래량": f"{settings.min_volume_usdt:,.0f} USDT",
        "최대포지션": f"{settings.max_position_usdt:,.2f} USDT",
    }


# ── 메인 애플리케이션 ─────────────────────────────────────────────────

class AutoCrypto:
    def __init__(self) -> None:
        self._exchange = settings.active_exchange
        # ── 팩토리로 거래소 모듈 선택 ────────────────────────
        self._scanner = _build_scanner(self._on_candle_closed)
        self._trader = _build_trader()
        # ── 공통 모듈 ────────────────────────────────────────
        self._indicator = BakktaIndicator()
        self._news = NewsFetcher()
        self._ai = AIAnalyzer()
        self._notifier = Notifier()
        self._db = ReasoningLogger()
        self._running = False

        # 뉴스 캐시 (scan_interval_sec마다 갱신)
        self._news_cache: NewsContext | None = None
        self._news_refresh_task: asyncio.Task | None = None

    async def start(self) -> None:
        logger.info(
            "autocrypto.starting",
            version="1.0.0",
            exchange=self._exchange,
        )

        if not self._db.health_check():
            logger.error("autocrypto.db_unreachable")
            sys.exit(1)

        await self._trader.init()

        self._news_refresh_task = asyncio.create_task(self._news_refresh_loop())
        self._running = True

        # 시작 알림
        status = {
            "상태": "시작됨",
            "시간": datetime.now(tz=timezone.utc).isoformat(),
            "타임프레임": settings.timeframe,
            "실거래": "✅ 활성화" if settings.trade_enabled else "🚫 비활성화 (DryRun)",
        }
        status.update(_exchange_display())
        await self._notifier.send_system_status(status)

        try:
            await self._scanner.start()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _on_candle_closed(self, symbol: str, df) -> None:
        """스캐너 캔들 완성 이벤트 → 파이프라인 태스크 생성 (병렬 처리)"""
        asyncio.create_task(self._process_symbol(symbol, df))

    async def _process_symbol(self, symbol: str, df) -> None:
        try:
            # 1. 기술적 지표 계산
            result = self._indicator.compute(symbol, df)
            if result is None:
                return

            # 약한 신호는 AI 호출 없이 조기 종료 (API 비용 절감)
            if not result.is_tradeable(min_score=55.0):
                return

            # 2. 지표 스냅샷 DB 저장 (백그라운드)
            asyncio.create_task(
                asyncio.to_thread(
                    self._db.log_indicator, symbol, result, self._exchange
                )
            )

            # 3. AI 분석 — 해당 코인 관련 뉴스만 전달
            coin = _extract_coin(symbol)
            news_ctx = (
                self._news_cache.for_coin(coin)
                if self._news_cache is not None
                else NewsContext.empty()
            )
            decision = await self._ai.analyze(symbol, result, news_ctx)
            if decision is None:
                return

            sig = decision.trade_signal
            conf_val = (
                sig.confidence
                if isinstance(sig.confidence, str)
                else sig.confidence.value
            )
            sig_val = (
                sig.signal if isinstance(sig.signal, str) else sig.signal.value
            )

            logger.info(
                "pipeline.signal",
                exchange=self._exchange,
                symbol=symbol,
                signal=sig_val,
                confidence=conf_val,
                score=sig.confidence_score,
                fallback=decision.is_fallback,
                retries=decision.retry_count,
            )

            # 폴백(NEUTRAL) 결정은 DB 기록만 남기고 알림/거래 없음
            if decision.is_fallback:
                await asyncio.to_thread(self._db.log_decision, decision)
                return

            # 4. DB 로깅
            decision_id = await asyncio.to_thread(self._db.log_decision, decision)

            # 5. HIGH 신뢰도만 알림 + 거래 실행
            if conf_val == "HIGH":
                await self._notifier.send_signal(decision)

                order = await self._trader.execute(decision)
                if order:
                    await asyncio.to_thread(
                        self._db.log_trade,
                        decision_id,
                        symbol,
                        order.side,
                        order.quantity,
                        order.entry_price,
                        order.stop_loss,
                        order.take_profit,
                        order.order_id,
                        self._exchange,
                    )
                    if not order.is_dry_run:
                        await asyncio.to_thread(
                            self._db.mark_decision_executed, decision_id
                        )

        except Exception as exc:
            logger.error(
                "pipeline.error",
                exchange=self._exchange,
                symbol=symbol,
                error=str(exc),
                exc_info=True,
            )

    async def _news_refresh_loop(self) -> None:
        """뉴스 + 공포·탐욕 지수를 scan_interval_sec마다 갱신"""
        while self._running:
            try:
                symbols = self._scanner.all_symbols()
                self._news_cache = await self._news.fetch_recent(
                    symbols=symbols[:20],
                    max_age_seconds=3600,
                )
                logger.info(
                    "news.cache_refreshed",
                    naver=len(self._news_cache.naver_items),
                    fear_greed=self._news_cache.fear_greed.score,
                )
            except Exception as exc:
                logger.warning("news.refresh_failed", error=str(exc))
            await asyncio.sleep(settings.scan_interval_sec)

    async def _shutdown(self) -> None:
        logger.info("autocrypto.shutting_down", exchange=self._exchange)
        self._running = False
        if self._news_refresh_task:
            self._news_refresh_task.cancel()
        await self._scanner.stop()
        await self._trader.close()
        await self._notifier.send_system_status(
            {"상태": "종료됨", "거래소": self._exchange}
        )


async def main() -> None:
    # Python 3.14 asyncio 슬로우 콜백 경고 임계값 상향 (기본 0.1s → 10s)
    # 네트워크 I/O 작업이 100ms를 넘으면 경고가 쏟아지므로 억제
    asyncio.get_running_loop().slow_callback_duration = 10.0

    app = AutoCrypto()

    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        logger.info("autocrypto.signal_received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows는 SIGTERM 미지원

    await app.start()


if __name__ == "__main__":
    # ── debugpy attach 모드 (F5 디버깅용) ──────────────────────────────
    # Python 3.14 + debugpy launch 모드는 wait_for_ready_to_run() hang 발생.
    # DEBUGPY_PORT 환경변수가 있으면 우리 프로세스가 먼저 시작한 뒤
    # VSCode가 attach하는 방식으로 우회합니다.
    _dbg_port = os.environ.get("DEBUGPY_PORT")
    if _dbg_port:
        import debugpy  # type: ignore[import]
        debugpy.listen(("localhost", int(_dbg_port)))
        print(f"[debugpy] Waiting for attach on localhost:{_dbg_port} ...", flush=True)
        debugpy.wait_for_client()

    asyncio.run(main())
