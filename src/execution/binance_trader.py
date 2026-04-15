"""
BinanceTrader — 바이낸스 주문 실행 모듈
────────────────────────────────────────
• TRADE_ENABLED=false 시 완전 dry-run (실제 주문 없음)
• 포지션 수 한도, USDT 금액 한도 적용
• python-binance AsyncClient 기반 시장가 주문

ACTIVE_EXCHANGE=binance 일 때 main.py가 이 모듈을 선택합니다.

OrderResult는 trader.py(UpbitTrader)와 공용으로 import합니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from tenacity import retry, stop_after_attempt, wait_exponential

from config import get_settings
from src.ai.schemas import AIDecision, SignalType
from src.execution.trader import OrderResult  # 공용 dataclass

logger = structlog.get_logger(__name__)


class BinanceTrader:
    """
    AI 매매 신호를 받아 바이낸스에 시장가 주문을 실행합니다.

    Usage:
        trader = BinanceTrader()
        await trader.init()
        result = await trader.execute(decision)
        await trader.close()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: AsyncClient | None = None
        self._open_positions: dict[str, OrderResult] = {}

    async def init(self) -> None:
        if not self._settings.binance_api_key:
            raise RuntimeError(
                "BINANCE_API_KEY가 설정되지 않았습니다. "
                ".env 파일에 BINANCE_API_KEY와 BINANCE_SECRET_KEY를 추가하세요."
            )
        self._client = await AsyncClient.create(
            api_key=self._settings.binance_api_key,
            api_secret=self._settings.binance_secret_key,
            testnet=self._settings.binance_testnet,
        )
        logger.info(
            "trader.initialized",
            exchange="binance",
            testnet=self._settings.binance_testnet,
            trade_enabled=self._settings.trade_enabled,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.close_connection()
        logger.info("trader.closed", exchange="binance")

    async def execute(self, decision: AIDecision) -> OrderResult | None:
        sig = decision.trade_signal
        sig_val = sig.signal if isinstance(sig.signal, str) else sig.signal.value

        if sig_val == SignalType.NEUTRAL.value:
            logger.debug("trader.skip_neutral", symbol=decision.symbol)
            return None

        if not self._can_open_position(decision.symbol):
            logger.info(
                "trader.position_limit_reached",
                symbol=decision.symbol,
                open=len(self._open_positions),
                max=self._settings.max_open_positions,
            )
            return None

        side = "BUY" if sig_val == SignalType.LONG.value else "SELL"
        quantity = self._calc_quantity(sig.entry_price)

        if quantity <= 0:
            logger.warning("trader.invalid_quantity", symbol=decision.symbol)
            return None

        if not self._settings.trade_enabled:
            result = OrderResult(
                symbol=decision.symbol,
                side=side,
                krw_amount=0.0,  # 바이낸스는 USDT 기준 → 0으로 표시
                quantity=quantity,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                order_id=f"DRY-{decision.symbol}-{int(datetime.now(tz=timezone.utc).timestamp())}",
                is_dry_run=True,
            )
            logger.info(
                "trader.dry_run",
                exchange="binance",
                symbol=decision.symbol,
                side=side,
                qty=quantity,
                entry=sig.entry_price,
            )
            self._open_positions[decision.symbol] = result
            return result

        return await self._place_order(decision, side, quantity)

    async def close_position(self, symbol: str) -> bool:
        if symbol not in self._open_positions:
            return False
        pos = self._open_positions[symbol]
        close_side = "SELL" if pos.side == "BUY" else "BUY"

        if not self._settings.trade_enabled:
            del self._open_positions[symbol]
            logger.info("trader.dry_close", exchange="binance", symbol=symbol)
            return True

        try:
            await self._client.create_order(  # type: ignore[union-attr]
                symbol=symbol,
                side=close_side,
                type="MARKET",
                quantity=pos.quantity,
            )
            del self._open_positions[symbol]
            logger.info("trader.position_closed", exchange="binance", symbol=symbol)
            return True
        except BinanceAPIException as exc:
            logger.error("trader.close_failed", symbol=symbol, error=str(exc))
            return False

    def get_open_positions(self) -> dict[str, OrderResult]:
        return dict(self._open_positions)

    # ── Private ───────────────────────────────────────────────────────

    def _can_open_position(self, symbol: str) -> bool:
        if symbol in self._open_positions:
            return False
        return len(self._open_positions) < self._settings.max_open_positions

    def _calc_quantity(self, price: float) -> float:
        """USDT 기반 포지션 크기 계산"""
        usdt_amount = min(
            self._settings.max_position_usdt,
            self._settings.max_position_usdt * self._settings.risk_per_trade_pct,
        )
        if price <= 0:
            return 0.0
        return round(usdt_amount / price, 5)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def _place_order(
        self, decision: AIDecision, side: str, quantity: float
    ) -> OrderResult:
        sig = decision.trade_signal
        symbol = decision.symbol

        try:
            order = await self._client.create_order(  # type: ignore[union-attr]
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=quantity,
            )
            order_id = str(order["orderId"])
            filled_price = float(
                order.get("fills", [{}])[0].get("price", sig.entry_price)
            )

            result = OrderResult(
                symbol=symbol,
                side=side,
                krw_amount=0.0,
                quantity=quantity,
                entry_price=filled_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                order_id=order_id,
                is_dry_run=False,
            )
            self._open_positions[symbol] = result
            logger.info(
                "trader.order_placed",
                exchange="binance",
                symbol=symbol,
                side=side,
                qty=quantity,
                price=filled_price,
                order_id=order_id,
            )
            return result

        except BinanceAPIException as exc:
            logger.error("trader.order_failed", symbol=symbol, error=str(exc))
            return OrderResult(
                symbol=symbol,
                side=side,
                krw_amount=0.0,
                quantity=quantity,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                order_id=None,
                is_dry_run=False,
                error=str(exc),
            )
