"""
UpbitTrader — 업비트 주문 실행 모듈
────────────────────────────────────
• TRADE_ENABLED=false 시 완전 dry-run (실제 주문 없음)
• 포지션 수 한도, KRW 금액 한도 적용
• pyupbit 시장가 매수/매도 (동기 API → asyncio.to_thread 래핑)

ACTIVE_EXCHANGE=upbit 일 때 main.py가 이 모듈을 선택합니다.
OrderResult dataclass는 BinanceTrader도 공용으로 import합니다.

업비트 주문 특성:
  - 시장가 매수: KRW 금액 지정 (buy_market_order)
  - 시장가 매도: 코인 수량 지정 (sell_market_order)
  - 주문 ID: order['uuid']
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pyupbit
import structlog

from config import get_settings
from src.ai.schemas import AIDecision, SignalType

logger = structlog.get_logger(__name__)


@dataclass
class OrderResult:
    symbol: str          # "KRW-BTC"
    side: str            # "BUY" / "SELL"
    krw_amount: float    # 투입 KRW (매수 시), 매도 시에는 entry_price * quantity
    quantity: float      # 코인 수량 (예상치 — 체결 후 갱신 가능)
    entry_price: float   # 진입 가격 (예상치)
    stop_loss: float
    take_profit: float
    order_id: str | None
    is_dry_run: bool
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def coin_ticker(self) -> str:
        """'KRW-BTC' → 'BTC'"""
        return self.symbol.replace("KRW-", "")


class UpbitTrader:
    """
    AI 매매 신호를 받아 업비트에 시장가 주문을 실행합니다.

    Usage:
        trader = UpbitTrader()
        await trader.init()
        result = await trader.execute(decision)
        await trader.close()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._upbit: pyupbit.Upbit | None = None
        self._open_positions: dict[str, OrderResult] = {}  # symbol → active order

    async def init(self) -> None:
        """업비트 클라이언트 초기화"""
        # pyupbit.Upbit() 생성은 가벼운 작업이므로 to_thread 불필요
        self._upbit = pyupbit.Upbit(
            access=self._settings.upbit_access_key,
            secret=self._settings.upbit_secret_key,
        )
        logger.info(
            "trader.initialized",
            exchange="upbit",
            trade_enabled=self._settings.trade_enabled,
            max_position_krw=self._settings.max_position_krw,
        )

    async def close(self) -> None:
        """정리 작업 (업비트는 persistent connection 없음)"""
        self._upbit = None
        logger.info("trader.closed")

    async def execute(self, decision: AIDecision) -> OrderResult | None:
        """
        AI 결정을 실행합니다.
        - NEUTRAL 신호: 건너뜀
        - 포지션 한도 초과: 건너뜀
        - TRADE_ENABLED=false: dry-run 기록만 남김
        """
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
        krw_amount = self._calc_krw_amount()

        if krw_amount <= 0:
            logger.warning("trader.invalid_krw_amount", symbol=decision.symbol)
            return None

        # 수량 예상값 (시장가이므로 실제 체결 수량과 차이 있을 수 있음)
        est_quantity = round(krw_amount / sig.entry_price, 8) if sig.entry_price > 0 else 0.0

        if not self._settings.trade_enabled:
            result = OrderResult(
                symbol=decision.symbol,
                side=side,
                krw_amount=krw_amount,
                quantity=est_quantity,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                order_id=f"DRY-{decision.symbol}-{int(datetime.now(tz=timezone.utc).timestamp())}",
                is_dry_run=True,
            )
            logger.info(
                "trader.dry_run",
                symbol=decision.symbol,
                side=side,
                krw=krw_amount,
                est_qty=est_quantity,
                entry=sig.entry_price,
            )
            self._open_positions[decision.symbol] = result
            return result

        return await self._place_order(decision, side, krw_amount, est_quantity)

    async def close_position(self, symbol: str) -> bool:
        """보유 코인 전량 시장가 매도로 포지션 청산"""
        if symbol not in self._open_positions:
            return False

        pos = self._open_positions[symbol]

        if not self._settings.trade_enabled:
            del self._open_positions[symbol]
            logger.info("trader.dry_close", symbol=symbol)
            return True

        try:
            # 실제 보유 수량 조회
            coin_ticker = pos.coin_ticker
            balance: float = await asyncio.to_thread(
                self._upbit.get_balance, coin_ticker  # type: ignore[union-attr]
            )
            if balance is None or balance <= 0:
                logger.warning("trader.no_balance_to_close", symbol=symbol)
                del self._open_positions[symbol]
                return False

            order = await asyncio.to_thread(
                self._upbit.sell_market_order,  # type: ignore[union-attr]
                symbol,
                balance,
            )
            if order and "uuid" in order:
                del self._open_positions[symbol]
                logger.info(
                    "trader.position_closed",
                    symbol=symbol,
                    qty=balance,
                    order_id=order["uuid"],
                )
                return True
            else:
                logger.error("trader.close_failed", symbol=symbol, response=order)
                return False

        except Exception as exc:
            logger.error("trader.close_exception", symbol=symbol, error=str(exc))
            return False

    def get_open_positions(self) -> dict[str, OrderResult]:
        return dict(self._open_positions)

    # ── Private ───────────────────────────────────────────────────────

    def _can_open_position(self, symbol: str) -> bool:
        if symbol in self._open_positions:
            return False  # 이미 포지션 보유 중
        return len(self._open_positions) < self._settings.max_open_positions

    def _calc_krw_amount(self) -> float:
        """
        리스크 비율 기반 투입 KRW 금액 계산.
        max_position_krw × risk_per_trade_pct (단, max_position_krw를 초과하지 않음)
        """
        amount = self._settings.max_position_krw * self._settings.risk_per_trade_pct
        return min(amount, self._settings.max_position_krw)

    async def _place_order(
        self,
        decision: AIDecision,
        side: str,
        krw_amount: float,
        est_quantity: float,
    ) -> OrderResult:
        sig = decision.trade_signal
        symbol = decision.symbol

        try:
            if side == "BUY":
                # 업비트 시장가 매수: KRW 금액 기준
                order = await asyncio.to_thread(
                    self._upbit.buy_market_order,  # type: ignore[union-attr]
                    symbol,
                    krw_amount,
                )
            else:
                # 업비트 시장가 매도: 코인 수량 기준
                # 실제 보유 수량으로 정확하게 매도
                coin_ticker = symbol.replace("KRW-", "")
                balance: float = await asyncio.to_thread(
                    self._upbit.get_balance,  # type: ignore[union-attr]
                    coin_ticker,
                )
                sell_qty = balance if balance else est_quantity
                order = await asyncio.to_thread(
                    self._upbit.sell_market_order,  # type: ignore[union-attr]
                    symbol,
                    sell_qty,
                )

            if order is None or "uuid" not in order:
                raise ValueError(f"업비트 주문 응답 이상: {order}")

            order_id: str = order["uuid"]
            # 체결 가격은 주문 직후 확정되지 않으므로 예상가 사용
            # (별도 체결 조회 로직 구현 권장)

            result = OrderResult(
                symbol=symbol,
                side=side,
                krw_amount=krw_amount,
                quantity=est_quantity,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                order_id=order_id,
                is_dry_run=False,
            )
            self._open_positions[symbol] = result

            logger.info(
                "trader.order_placed",
                symbol=symbol,
                side=side,
                krw=krw_amount,
                est_qty=est_quantity,
                order_id=order_id,
            )
            return result

        except Exception as exc:
            logger.error("trader.order_failed", symbol=symbol, error=str(exc))
            return OrderResult(
                symbol=symbol,
                side=side,
                krw_amount=krw_amount,
                quantity=est_quantity,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                order_id=None,
                is_dry_run=False,
                error=str(exc),
            )
