"""
Notice Monitor
──────────────
\uc5c5\ube44\ud2b8 \uacf5\uc9c0\uc0ac\ud56d API\ub97c \uc8fc\uae30\uc801\uc73c\ub85c \ud3f4\ub9c1\ud558\uc5ec \uc2e0\uaddc \uacf5\uc9c0\ub97c \uc54c\ub9bd\ud569\ub2c8\ub2e4.

\uc8fc\uc694 \uae30\ub2a5:
  1. \uc5c5\ube44\ud2b8 /v1.0/market/notice API \ud3f4\ub9c1
  2. \ud0a4\uc6cc\ub4dc \ud544\ud130\ub9c1 (NOTICE_KEYWORDS \ud658\uacbd\ubcc0\uc218)
  3. \uc911\ubcf5 \uc81c\uac70 (\uc774\ubbf8 \uc57c\ub2e8\ud55c \ud56d\ubaa9 \uc81c\uc678)
  4. \ucab5\ub3d9 \uc2e4\ud589 \uc2dc false-positive \ubc29\uc9c0 (_initialized \ud50c\ub798\uadf8)
  5. BTC USD \ub77c\uc6b4\ub4dc\ud53c\uac70 \ub3cc\ud30c \uac10\uc9c0 (70K/75K/80K/90K/100K \ub4f1)
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

from config import get_settings

logger = structlog.get_logger(__name__)

# \ud0a4\uc6cc\ub4dc \ud544\ud130\ub9c1 — \ud658\uacbd\ubcc0\uc218 NOTICE_KEYWORDS \ubbf8\uc124\uc815 \uc2dc \uc774 \ubaa9\ub85d\uc744 \uc0ac\uc6a9
_DEFAULT_KEYWORDS: list[str] = [
    "\uc0c1\uc7a5", "\uc2e0\uaddc", "\uc9c0\uc6d0", "\uc5c5\ub370\uc774\ud2b8",
    "\uc8fc\uc758", "\uac70\ub798\uc9c0\uc6d0", "\uc2dc\uc7a5", "\uc21c\uc704",
    "\uc815\uc9c0", "\uc810\uac80", "\uc8fc\uc758", "list", "support",
]

# BTC USD \ub77c\uc6b4\ub4dc\ud53c\uac70 \ub808\ubca8 (\uc62c\ub9bc\ucc28\uc21c, \ub2e4\uc74c \ub808\ubca8 \ub3cc\ud30c \uc2dc \uc54c\ub9bc)
_BTC_ROUND_LEVELS: list[int] = [
    30_000, 35_000, 40_000, 45_000, 50_000,
    55_000, 60_000, 65_000, 70_000, 75_000,
    80_000, 85_000, 90_000, 95_000, 100_000,
    110_000, 120_000, 130_000, 150_000, 200_000,
]


@dataclass
class NoticeItem:
    """
    \uc5c5\ube44\ud2b8 \uacf5\uc9c0 \ud56d\ubaa9.
    notice_id : \uacf5\uc9c0 \uace0\uc720 ID (\uc911\ubcf5 \uc81c\uac70\uc6a9)
    title     : \uacf5\uc9c0 \uc81c\ubaa9
    url       : \uacf5\uc9c0 URL
    """
    notice_id: str
    title: str
    url: str


@dataclass
class RoundFigureAlert:
    """
    BTC \ub77c\uc6b4\ub4dc\ud53c\uac70 \ub3cc\ud30c \uc54c\ub9bc.
    level     : \ub3cc\ud30c\ud55c \ub77c\uc6b4\ub4dc\ud53c\uac70 ($)
    direction : "above" | "below"
    price     : \uc2e4\uc81c \ud604\uc7ac\uac00 (USD)
    """
    level: int
    direction: str
    price: float


class NoticeMonitor:
    """
    \uc5c5\ube44\ud2b8 \uacf5\uc9c0 + BTC \ub77c\uc6b4\ub4dc\ud53c\uac70 \ubaa8\ub2c8\ud130.

    \uccab \ud0c0\uc784 check_notices() \ud638\ucd9c \uc2dc\ub294 \ud604\uc7ac \uacf5\uc9c0\ub97c \uc2a4\ub0c5\uc0f7\uc73c\ub85c\ub9cc \uc800\uc7a5\ud558\uace0
    \uc54c\ub9bc\uc744 \ubc1c\uc1a1\ud558\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4 (\uc2dc\uc791 \uc2dc false-positive \ubc29\uc9c0).
    """

    _UPBIT_NOTICE_URL = "https://api.upbit.com/v1/market/notice"

    def __init__(self) -> None:
        self._seen_ids:   set[str] = set()
        self._initialized: bool    = False
        # BTC USD: \ub9c8\uc9c0\ub9c9\uc73c\ub85c \ud655\uc778\ud55c \ub808\ubca8 \uc778\ub371\uc2a4 (\uc544\ub798\uc5d0\uc11c \uc704\ub85c \uc21c\uc11c\uc758 _BTC_ROUND_LEVELS)
        self._last_btc_level_idx: int | None = None

    # ── \uacf5\uac1c API ────────────────────────────────────────────────────────

    async def check_notices(self) -> list[NoticeItem]:
        """
        \uc5c5\ube44\ud2b8 \uacf5\uc9c0\uc0ac\ud56d API\ub97c \ud3f4\ub9c1\ud558\uc5ec \uc2e0\uaddc \uacf5\uc9c0\ub97c \ubc18\ud658\ud569\ub2c8\ub2e4.

        Returns:
            \uc2e0\uaddc \uacf5\uc9c0 \ubaa9\ub85d (\uccab \ud0c0\uc784 \ud638\ucd9c \uc2dc\ub294 \ube48 \ub9ac\uc2a4\ud2b8)
        """
        try:
            raw_items = await self._fetch_raw_notices()
        except Exception as exc:
            logger.warning("notice_monitor.fetch_failed", error=str(exc))
            return []

        keywords = self._get_keywords()
        new_items: list[NoticeItem] = []

        for item in raw_items:
            nid   = str(item.get("id", ""))
            title = item.get("title", "")
            url   = item.get("url", "") or f"https://upbit.com/service_center/notice?id={nid}"

            if not nid:
                continue

            # \ud0a4\uc6cc\ub4dc \ud544\ud130\ub9c1
            if keywords and not any(kw.lower() in title.lower() for kw in keywords):
                self._seen_ids.add(nid)   # \ud3c9\uc18c \ud655\uc778 \ud6c4 \uc81c\uc678
                continue

            if nid not in self._seen_ids:
                new_items.append(NoticeItem(notice_id=nid, title=title, url=url))
                self._seen_ids.add(nid)

        if not self._initialized:
            # \uccab \uc2e4\ud589: \uc2e0\uaddc \ubaa9\ub85d\ub9cc \uc800\uc7a5\ud558\uace0 \ubc18\ud658\ud558\uc9c0 \uc54a\uc74c
            self._initialized = True
            return []

        return new_items

    async def check_btc_round_figures(
        self, btc_usd_price: float | None
    ) -> list[RoundFigureAlert]:
        """
        BTC USD \ub77c\uc6b4\ub4dc\ud53c\uac70 \ub3cc\ud30c \uc5ec\ubd80\ub97c \ud655\uc778\ud569\ub2c8\ub2e4.

        btc_usd_price: \uc5c5\ube44\ud2b8 BTC\ub294 KRW\uc774\ubbc0\ub85c market_fetcher.fetch_btc_usd_price()\ub85c \uc218\uc9d1\ud55c USD \uac12\uc744 \uc804\ub2ec.
        """
        if btc_usd_price is None or btc_usd_price <= 0:
            return []

        # \ud604\uc7ac \uac00\uaca9\uc774 \uc5b4\ub290 \ub808\ubca8 \ubc14\ub85c \uc544\ub798\uc5d0 \uc788\ub294\uc9c0 \ud655\uc778
        current_idx: int | None = None
        for i, level in enumerate(_BTC_ROUND_LEVELS):
            if btc_usd_price < level:
                # \ud604\uc7ac\uac00\ub294 level \uc544\ub798\uc5d0 \uc788\uc74c — \ub85c\uc6b0\uc5b4 \ub808\ubca8 \ubcc0\uc624 \uc911
                current_idx = i - 1  # \uc9c1\uc804 \ub808\ubca8 \uc778\ub371\uc2a4
                break
        else:
            # \ubaa8\ub4e0 \ub808\ubca8 \uc704 (200K \ucd08\uacfc)
            current_idx = len(_BTC_ROUND_LEVELS) - 1

        if self._last_btc_level_idx is None:
            self._last_btc_level_idx = current_idx
            return []

        alerts: list[RoundFigureAlert] = []

        if current_idx is not None and current_idx != self._last_btc_level_idx:
            direction = "above" if current_idx > self._last_btc_level_idx else "below"
            # \ub123\uc740 \ub808\ubca8\uc774 \uc5b4\ub290 \ud6c4 \ub4e4\uc5b4\uc634: \ub85c\uc6b0\uc5b4 \ub808\ubca8 \ub3cc\ud30c \ud655\uc778
            if direction == "above" and current_idx >= 0:
                level = _BTC_ROUND_LEVELS[current_idx]
                alerts.append(RoundFigureAlert(level=level, direction="above", price=btc_usd_price))
            elif direction == "below" and current_idx >= 0:
                level = _BTC_ROUND_LEVELS[current_idx + 1] if current_idx + 1 < len(_BTC_ROUND_LEVELS) else 0
                if level:
                    alerts.append(RoundFigureAlert(level=level, direction="below", price=btc_usd_price))

            self._last_btc_level_idx = current_idx

        return alerts

    # ── \ub0b4\ubd80 \ud5ec\ud37c ──────────────────────────────────────────────────────

    async def _fetch_raw_notices(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                self._UPBIT_NOTICE_URL,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            # \uc5c5\ube44\ud2b8 API \ub9ac\uc2a4\ud3f0\uc2a4 \uc8fc\uc694 \ud568\uc218: {"data": {"list": [...]}} \ub610\ub294 \ub9ac\uc2a4\ud2b8 \uc9c1\ubc18\ud658
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("data", {}).get("list", []) if isinstance(data.get("data"), dict) else data.get("list", [])
            return []

    @staticmethod
    def _get_keywords() -> list[str]:
        """
        NOTICE_KEYWORDS \ud658\uacbd\ubcc0\uc218\uc5d0\uc11c \ud0a4\uc6cc\ub4dc\ub97c \uc77d\uc5b4\uc635\ub2c8\ub2e4.
        \ubbf8\uc124\uc815 \uc2dc \uae30\ubcf8 \ubaa9\ub85d(_DEFAULT_KEYWORDS)\ub97c \uc0ac\uc6a9\ud569\ub2c8\ub2e4.
        """
        try:
            settings = get_settings()
            raw = getattr(settings, "notice_keywords", None)
            if raw:
                return [kw.strip() for kw in raw.split(",") if kw.strip()]
        except Exception:
            pass
        return _DEFAULT_KEYWORDS
