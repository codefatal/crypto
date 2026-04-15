"""
src/data — lazy import

BinanceScanner는 python-binance(→ dateparser → regex)를 module-level에서
로드하므로, ACTIVE_EXCHANGE=upbit 환경에서도 무조건 import되던 문제를 방지합니다.
실제 클래스는 처음 참조될 때 로드됩니다.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 타입 체커(mypy, pyright)에만 노출 — 런타임에는 import 안 함
    from .binance_scanner import BinanceScanner
    from .upbit_scanner import UpbitScanner
    from .news_fetcher import NewsFetcher


def __getattr__(name: str):
    if name == "UpbitScanner":
        from .upbit_scanner import UpbitScanner
        return UpbitScanner
    if name == "BinanceScanner":
        from .binance_scanner import BinanceScanner
        return BinanceScanner
    if name == "NewsFetcher":
        from .news_fetcher import NewsFetcher
        return NewsFetcher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["UpbitScanner", "BinanceScanner", "NewsFetcher"]
