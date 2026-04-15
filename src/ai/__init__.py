"""
src/ai — lazy import
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .analyzer import AIAnalyzer
    from .schemas import TradeSignal, AIDecision


def __getattr__(name: str):
    if name == "AIAnalyzer":
        from .analyzer import AIAnalyzer
        return AIAnalyzer
    if name in ("TradeSignal", "AIDecision"):
        from .schemas import TradeSignal, AIDecision
        return TradeSignal if name == "TradeSignal" else AIDecision
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AIAnalyzer", "TradeSignal", "AIDecision"]
