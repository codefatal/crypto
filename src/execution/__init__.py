"""
src/execution — lazy import

BinanceTrader도 python-binance를 module-level에서 로드하므로
ACTIVE_EXCHANGE=upbit 환경에서의 불필요한 import를 방지합니다.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .trader import UpbitTrader, OrderResult
    from .binance_trader import BinanceTrader
    from .notifier import Notifier
    from .logger import ReasoningLogger


def __getattr__(name: str):
    if name in ("UpbitTrader", "OrderResult"):
        from .trader import UpbitTrader, OrderResult
        return UpbitTrader if name == "UpbitTrader" else OrderResult
    if name == "BinanceTrader":
        from .binance_trader import BinanceTrader
        return BinanceTrader
    if name == "Notifier":
        from .notifier import Notifier
        return Notifier
    if name == "ReasoningLogger":
        from .logger import ReasoningLogger
        return ReasoningLogger
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["UpbitTrader", "BinanceTrader", "OrderResult", "Notifier", "ReasoningLogger"]
