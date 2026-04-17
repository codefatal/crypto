"""
Notifier
─────────
Discord Webhook + Telegram Bot API로 매매 신호 알림 발송.
각 플랫폼별 Embed/Markdown 포맷 적용.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import structlog

from config import get_settings
from src.ai.schemas import AIDecision, SignalType
from src.data.market_fetcher import MarketBriefing
from src.data.news_fetcher import DominanceData, MarketOverviewItem
from src.indicator.technical import ExtremeSignal

# Discord 웹훅은 웹훅 URL별로 30 req/min 제한이 있음.
# 동시 발송을 직렬화하여 burst 429를 방지한다.
_DISCORD_SEMAPHORE = asyncio.Semaphore(1)

# Telegram Bot API: 동일 채팅에 초당 1건, 분당 20건 제한.
# 동시 발송을 직렬화하여 burst 429를 방지한다.
_TELEGRAM_SEMAPHORE = asyncio.Semaphore(1)

# 업비트 KRW 마켓 심볼 → 한국어 코인명 매핑
# 인코딩 이슈를 방지하기 위해 모든 한글을 \uXXXX 유니코드 이스케이프로 표기
_COIN_NAMES: dict[str, str] = {
    "KRW-BTC":   "\ube44\ud2b8\ucf54\uc778",
    "KRW-ETH":   "\uc774\ub354\ub9ac\uc6c0",
    "KRW-XRP":   "\ub9ac\ud50c",
    "KRW-SOL":   "\uc194\ub77c\ub098",
    "KRW-DOGE":  "\ub3c4\uc9c0\ucf54\uc778",
    "KRW-ADA":   "\uc5d0\uc774\ub2e4",
    "KRW-AVAX":  "\uc544\ubc1c\ub780\uccb4",
    "KRW-DOT":   "\ud3f4\uce74\ub2f7",
    "KRW-LINK":  "\uccb4\uc778\ub9c1\ud06c",
    "KRW-TRX":   "\ud2b8\ub860",
    "KRW-SHIB":  "\uc2dc\ubc14\uc774\ub204",
    "KRW-BCH":   "\ube44\ud2b8\ucf54\uc778\uce90\uc2dc",
    "KRW-ETC":   "\uc774\ub354\ub9ac\uc6c0\ud074\ub798\uc2dd",
    "KRW-NEAR":  "\ub2c8\uc5b4\ud504\ub85c\ud1a0\ucf5c",
    "KRW-ATOM":  "\ucf54\uc2a4\ubaa8\uc2a4",
    "KRW-UNI":   "\uc720\ub2c8\uc2a4\uc651",
    "KRW-ICP":   "\uc778\ud130\ub137\ucef4\ud4e8\ud130",
    "KRW-APT":   "\uc571\ud1a0\uc2a4",
    "KRW-SUI":   "\uc218\uc774",
    "KRW-ARB":   "\uc544\ube44\ud2b8\ub7fc",
    "KRW-OP":    "\uc635\ud2f0\ubbf8\uc998",
    "KRW-FIL":   "\ud30c\uc77c\ucf54\uc778",
    "KRW-SAND":  "\uc0cc\ub4dc\ubc15\uc2a4",
    "KRW-MANA":  "\ub514\uc13c\ud2b8\ub7f4\ub79c\ub4dc",
    "KRW-AAVE":  "\uc5d0\uc774\ube0c",
    "KRW-VET":   "\ube44\uccb4\uc778",
    "KRW-XLM":   "\uc2a4\ud154\ub77c\ub8e8\uba58",
    "KRW-HBAR":  "\ud5e4\ub370\ub77c",
    "KRW-ALGO":  "\uc54c\uace0\ub79c\ub4dc",
    "KRW-STX":   "\uc2a4\ud0dd\uc2a4",
    "KRW-GRT":   "\ub354\uadf8\ub798\ud504",
    "KRW-CHZ":   "\uce60\ub9ac\uc988",
    "KRW-COMP":  "\ucef4\ud30c\uc6b4\ub4dc",
    "KRW-BAT":   "\ubca0\uc774\uc9c1\uc5b4\ud150\uc158\ud1a0\ud070",
    "KRW-ICX":   "\uc544\uc774\ucf58",
    "KRW-QTUM":  "\ud000\ud140",
    "KRW-XTZ":   "\ud14c\uc870\uc2a4",
    "KRW-IOTA":  "\uc544\uc774\uc624\ud0c0",
    "KRW-IOST":  "\uc544\uc774\uc624\uc5d0\uc2a4\ud2f0",
    "KRW-ONT":   "\uc628\ud1a8\ub85c\uc9c0",
    "KRW-ZIL":   "\uc9c8\ub9ac\uce74",
    "KRW-STEEM": "\uc2a4\ud300",
    "KRW-SC":    "\uc2dc\uc544\ucf54\uc778",
    "KRW-LSK":   "\ub9ac\uc2a4\ud06c",
    "KRW-KAVA":  "\uce74\ubc14",
    "KRW-MTL":   "\uba54\ud0c8",
    "KRW-GLM":   "\uace8\ub818",
    "KRW-SNT":   "\uc2a4\ud14c\uc774\ud130\uc2a4",
}


def _symbol_display(symbol: str) -> str:
    """'KRW-BTC' → 'KRW-BTC(비트코인)'  /  매핑 없으면 원본 반환"""
    name = _COIN_NAMES.get(symbol)
    return f"{symbol}({name})" if name else symbol


def _fmt_change(rate: float | None) -> str | None:
    """24h 등락률 포맷. None이면 None 반환(필드 생략용).
    rate는 소수(0.0512 = +5.12%). 반환 예: '▲ +5.12%' / '▼ -3.40%'"""
    if rate is None:
        return None
    arrow = "\u25b2" if rate >= 0 else "\u25bc"  # ▲ / ▼
    return f"{arrow} {rate:+.2%}"


def _fmt_price(price: float | None) -> str:
    """\uac00\uaca9 \ud3ec\ub9f7. None \ub610\ub294 0\uc774\uba74 'N/A' \ubc18\ud658."""
    if price is None or price == 0:
        return "N/A"
    if price >= 1_000:
        return f"{price:,.2f}"
    return f"{price:.4f}"


def _fmt_chg_row(q_name: str, price: float | None, chg_pct: float | None) -> str:
    """\ud55c \uc904\uc9dc\ub9ac \uc2dc\uc138 \uc694\uc57d (Discord/Telegram \uacf5\uc6a9)."""
    price_str = _fmt_price(price)
    if chg_pct is None:
        return f"{q_name}: `{price_str}`"
    arrow = "\u25b2" if chg_pct >= 0 else "\u25bc"
    return f"{q_name}: `{price_str}` {arrow} `{chg_pct:+.2f}%`"

logger = structlog.get_logger(__name__)

# 신호별 색상 코드 (Discord embed color)
_SIGNAL_COLOR = {
    SignalType.LONG: 0x00FF88,    # 초록
    SignalType.SHORT: 0xFF4444,   # 빨강
    SignalType.NEUTRAL: 0x888888, # 회색
}

_SIGNAL_EMOJI = {
    SignalType.LONG: "📈",
    SignalType.SHORT: "📉",
    SignalType.NEUTRAL: "⏸️",
}

_CONFIDENCE_EMOJI = {
    "HIGH": "🔥",
    "MEDIUM": "⚡",
    "LOW": "💧",
}


class Notifier:
    """
    Discord + Telegram 동시 발송.
    한 채널이 실패해도 다른 채널 발송은 계속됩니다.

    Usage:
        notifier = Notifier()
        await notifier.send_signal(decision)
        await notifier.send_error("심각한 오류 발생: ...")
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    # ── Public ────────────────────────────────────────────────────────

    async def send_signal(
        self, decision: AIDecision, change_rate: float | None = None
    ) -> None:
        """매매 신호를 Discord + Telegram으로 동시 발송"""
        await asyncio.gather(
            self._discord_signal(decision, change_rate),
            self._telegram_signal(decision, change_rate),
            return_exceptions=True,
        )

    async def send_error(self, message: str) -> None:
        """에러 알림 발송"""
        await asyncio.gather(
            self._discord_plain(f"🚨 **에러 발생**\n```{message}```",
                                self._settings.discord_webhook_url),
            self._telegram_plain(f"🚨 에러 발생\n`{message}`"),
            return_exceptions=True,
        )

    async def send_system_status(self, status: dict) -> None:
        """시스템 상태 알림 (주기적 헬스체크)"""
        lines = [f"• {k}: {v}" for k, v in status.items()]
        msg = "📊 **시스템 상태**\n" + "\n".join(lines)
        await asyncio.gather(
            self._discord_plain(msg, self._settings.discord_webhook_url),
            self._telegram_plain(msg.replace("**", "*")),
            return_exceptions=True,
        )

    async def send_news_digest(self, digest_text: str) -> None:
        """시작 시 뉴스 다이제스트를 Discord + Telegram으로 발송"""
        await asyncio.gather(
            self._discord_plain(digest_text[:2000], self._settings.discord_webhook_url),
            self._telegram_plain(digest_text[:4096]),
            return_exceptions=True,
        )

    async def send_signal_brief(
        self, decision: AIDecision, change_rate: float | None = None
    ) -> None:
        """BTC 간단 알림 — HIGH가 아닌 신뢰도에서 현재가·신호·근거 중심으로 발송"""
        await asyncio.gather(
            self._discord_signal_brief(decision, change_rate),
            self._telegram_signal_brief(decision, change_rate),
            return_exceptions=True,
        )

    async def send_dominance(self, data: DominanceData) -> None:
        """BTC 도미넌스 리포트를 Discord + Telegram으로 발송"""
        await asyncio.gather(
            self._discord_dominance(data),
            self._telegram_dominance(data),
            return_exceptions=True,
        )

    async def send_market_overview(
        self,
        gainers: list[MarketOverviewItem],
        losers: list[MarketOverviewItem],
    ) -> None:
        """급등/급락 상위 코인 현황을 Discord + Telegram으로 발송"""
        await asyncio.gather(
            self._discord_market_overview(gainers, losers),
            self._telegram_market_overview(gainers, losers),
            return_exceptions=True,
        )

    async def send_breakout_alert(
        self,
        symbol: str,
        triggered_conditions: list[dict],
        current_values: dict[str, float],
        change_rate: float | None = None,
    ) -> None:
        """규칙 기반 돌파 알림을 Discord + Telegram으로 발송"""
        await asyncio.gather(
            self._discord_breakout(symbol, triggered_conditions, current_values, change_rate),
            self._telegram_breakout(symbol, triggered_conditions, current_values, change_rate),
            return_exceptions=True,
        )

    async def send_spike_alert(
        self,
        symbol: str,
        change_pct: float,
        current_price: float,
        ref_price: float,
        change_rate_24h: float | None = None,
    ) -> None:
        """급등/급락(±10% 이상) 알림을 Discord + Telegram으로 발송"""
        await asyncio.gather(
            self._discord_spike(symbol, change_pct, current_price, ref_price, change_rate_24h),
            self._telegram_spike(symbol, change_pct, current_price, ref_price, change_rate_24h),
            return_exceptions=True,
        )

    async def send_market_briefing(self, briefing: MarketBriefing) -> None:
        """\uac70\uc2dc\uacbd\uc81c \ube0c\ub9ac\ud551 (KST 09:00 / 22:30 \uc2a4\ucf00\uc904\ub7ec)\uc744 Discord + Telegram\uc73c\ub85c \ubc1c\uc1a1"""
        await asyncio.gather(
            self._discord_market_briefing(briefing),
            self._telegram_market_briefing(briefing),
            return_exceptions=True,
        )

    async def send_breaking_news(
        self,
        title: str,
        url: str,
        source: str = "\uc5c5\ube44\ud2b8 \uacf5\uc9c0",
    ) -> None:
        """\uc5c5\ube44\ud2b8 \uc2e0\uaddc \uacf5\uc9c0 / BTC \ub77c\uc6b4\ub4dc\ud53c\uac70 \ub3cc\ud30c \uc18d\ubcf4\ub97c Discord + Telegram\uc73c\ub85c \ubc1c\uc1a1"""
        await asyncio.gather(
            self._discord_breaking_news(title, url, source),
            self._telegram_breaking_news(title, url, source),
            return_exceptions=True,
        )

    async def send_extreme_alert(
        self,
        symbol: str,
        signal: ExtremeSignal,
        change_rate: float | None = None,
    ) -> None:
        """\ud328\ub2c9\uc140 / \uc800\ud3c9\uac00 / \ubc18\ub4f1 \uc2e0\ud638\ub97c Discord + Telegram\uc73c\ub85c \ubc1c\uc1a1"""
        await asyncio.gather(
            self._discord_extreme(symbol, signal, change_rate),
            self._telegram_extreme(symbol, signal, change_rate),
            return_exceptions=True,
        )

    # ── Discord ───────────────────────────────────────────────────────

    async def _discord_breakout(
        self,
        symbol: str,
        triggered_conditions: list[dict],
        current_values: dict[str, float],
        change_rate: float | None = None,
    ) -> None:
        webhook_url = self._settings.discord_signal_webhook_url or \
                      self._settings.discord_webhook_url
        if not webhook_url:
            return

        cond_lines = "\n".join(f"• {c['name']}" for c in triggered_conditions)
        count = len(triggered_conditions)

        def _fmt(v: float) -> str:
            return "N/A" if v != v else f"{v:.2f}"  # NaN check

        summary = (
            f"RSI `{_fmt(current_values.get('rsi', float('nan')))}` | "
            f"MACD `{_fmt(current_values.get('macd', float('nan')))}` | "
            f"StochK `{_fmt(current_values.get('stoch_rsi_k', float('nan')))}` | "
            f"VolRatio `{_fmt(current_values.get('volume_ratio', float('nan')))}x` | "
            f"ADX `{_fmt(current_values.get('adx', float('nan')))}`"
        )

        fields = []
        chg = _fmt_change(change_rate)
        if chg:
            fields.append({
                "name": "📊 24h 등락",
                "value": f"`{chg}`",
                "inline": True,
            })
        fields += [
            {
                "name": f"✅ 충족된 조건 ({count}/5)",
                "value": cond_lines or "없음",
                "inline": False,
            },
            {
                "name": "📈 전체 지표 현황",
                "value": summary,
                "inline": False,
            },
        ]

        embed = {
            "title": f"🚀 [돌파 감지] {_symbol_display(symbol)} 기술적 조건 충족!",
            "color": 0x00BFFF,  # 하늘색 — AI 신호(초록/빨강)와 구별
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "fields": fields,
            "footer": {"text": "Rule-based Breakout Alert"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_signal_brief(
        self, decision: AIDecision, change_rate: float | None = None
    ) -> None:
        webhook_url = self._settings.discord_signal_webhook_url or \
                      self._settings.discord_webhook_url
        if not webhook_url:
            return

        sig = decision.trade_signal
        signal_type = SignalType(sig.signal) if isinstance(sig.signal, str) else sig.signal
        color = _SIGNAL_COLOR.get(signal_type, 0x888888)
        emoji = _SIGNAL_EMOJI.get(signal_type, "")
        conf_val = sig.confidence if isinstance(sig.confidence, str) else sig.confidence.value
        sig_val = sig.signal if isinstance(sig.signal, str) else sig.signal.value
        conf_emoji = _CONFIDENCE_EMOJI.get(conf_val, "")

        fields = [
            {
                "name": f"{conf_emoji} 신뢰도",
                "value": f"{conf_val} ({sig.confidence_score:.1f}/100)",
                "inline": True,
            },
            {
                "name": "📌 현재가",
                "value": f"`{sig.entry_price:,.2f}`",
                "inline": True,
            },
        ]
        chg = _fmt_change(change_rate)
        if chg:
            fields.append({
                "name": "📊 24h 등락",
                "value": f"`{chg}`",
                "inline": True,
            })
        fields.append({
            "name": "📝 판단 근거",
            "value": sig.reasoning[:512],
            "inline": False,
        })

        embed = {
            "title": f"₿ {_symbol_display(decision.symbol)} — {sig_val} (BTC 알림)",
            "color": color,
            "timestamp": decision.timestamp,
            "fields": fields,
            "footer": {"text": f"{emoji} {conf_val} | {decision.model_version}"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_market_overview(
        self,
        gainers: list[MarketOverviewItem],
        losers: list[MarketOverviewItem],
    ) -> None:
        webhook_url = self._settings.discord_webhook_url or \
                      self._settings.discord_signal_webhook_url
        if not webhook_url:
            return
        if not gainers and not losers:
            return

        def _fmt_rows(items: list[MarketOverviewItem]) -> str:
            lines = []
            for i, item in enumerate(items, 1):
                name = _symbol_display(item.symbol)
                pct  = item.change_rate * 100
                sign = "▲" if pct >= 0 else "▼"
                lines.append(f"`{i}.` {name}  {sign} **{pct:+.2f}%**")
            return "\n".join(lines) if lines else "데이터 없음"

        fields = []
        if gainers:
            fields.append({
                "name": "🔥 급등 TOP 5",
                "value": _fmt_rows(gainers),
                "inline": False,
            })
        if losers:
            fields.append({
                "name": "💥 급락 TOP 5",
                "value": _fmt_rows(losers),
                "inline": False,
            })

        embed = {
            "title": "📊 시장 등락률 현황 (24h)",
            "color": 0x5865F2,  # 인디고 — 기존 알림과 구별
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "fields": fields,
            "footer": {"text": "Upbit KRW 전 종목 기준"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_dominance(self, data: DominanceData) -> None:
        webhook_url = self._settings.discord_webhook_url
        if not webhook_url:
            return

        total_b = data.total_market_cap_usd / 1e9

        fields = [
            {
                "name": "₿ BTC 도미넌스",
                "value": f"**{data.btc_dominance:.2f}%**",
                "inline": True,
            },
        ]
        if data.eth_dominance > 0:
            fields.append({
                "name": "Ξ ETH 도미넌스",
                "value": f"**{data.eth_dominance:.2f}%**",
                "inline": True,
            })
        if total_b > 0:
            change_str = ""
            if data.market_cap_change_24h != 0:
                change_emoji = "📈" if data.market_cap_change_24h > 0 else "📉"
                change_str = f" ({change_emoji} {data.market_cap_change_24h:+.2f}% 24h)"
            fields.append({
                "name": "💰 전체 시총",
                "value": f"${total_b:,.1f}B{change_str}",
                "inline": False,
            })

        embed = {
            "title": "🌐 BTC 도미넌스 리포트",
            "color": 0xF7931A,
            "timestamp": data.updated_at or datetime.now(tz=timezone.utc).isoformat(),
            "fields": fields,
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_signal(
        self, decision: AIDecision, change_rate: float | None = None
    ) -> None:
        webhook_url = self._settings.discord_signal_webhook_url or \
                      self._settings.discord_webhook_url
        if not webhook_url:
            return

        sig = decision.trade_signal
        signal_type = SignalType(sig.signal) if isinstance(sig.signal, str) else sig.signal
        color = _SIGNAL_COLOR.get(signal_type, 0x888888)
        emoji = _SIGNAL_EMOJI.get(signal_type, "")
        conf_emoji = _CONFIDENCE_EMOJI.get(
            sig.confidence if isinstance(sig.confidence, str) else sig.confidence.value,
            ""
        )

        conf_val = sig.confidence if isinstance(sig.confidence, str) else sig.confidence.value
        sig_val = sig.signal if isinstance(sig.signal, str) else sig.signal.value

        risks = "\n".join(f"• {r}" for r in sig.key_risks) if sig.key_risks else "없음"

        fields = [
            {
                "name": f"{conf_emoji} 신뢰도",
                "value": f"{conf_val} ({sig.confidence_score:.1f}/100)",
                "inline": True,
            },
            {
                "name": "📌 현재가",
                "value": f"`{sig.entry_price:,.6f}`",
                "inline": True,
            },
        ]
        chg = _fmt_change(change_rate)
        if chg:
            fields.append({
                "name": "📊 24h 등락",
                "value": f"`{chg}`",
                "inline": True,
            })
        fields += [
            {
                "name": "🔴 손절",
                "value": f"`{sig.stop_loss:,.6f}`",
                "inline": True,
            },
            {
                "name": "🟢 익절",
                "value": f"`{sig.take_profit:,.6f}`",
                "inline": True,
            },
            {
                "name": "📰 뉴스 영향",
                "value": sig.news_impact,
                "inline": True,
            },
            {
                "name": "⏱️ 분석 소요",
                "value": f"{decision.analysis_duration_ms}ms",
                "inline": True,
            },
            {
                "name": "📝 판단 근거",
                "value": sig.reasoning[:1024],
                "inline": False,
            },
            {
                "name": "⚠️ 주요 리스크",
                "value": risks[:512],
                "inline": False,
            },
        ]

        embed = {
            "title": f"{emoji} {_symbol_display(decision.symbol)} — {sig_val}",
            "color": color,
            "timestamp": decision.timestamp,
            "fields": fields,
            "footer": {"text": f"Model: {decision.model_version}"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_spike(
        self,
        symbol: str,
        change_pct: float,
        current_price: float,
        ref_price: float,
        change_rate_24h: float | None = None,
    ) -> None:
        webhook_url = self._settings.discord_signal_webhook_url or \
                      self._settings.discord_webhook_url
        if not webhook_url:
            return

        is_surge = change_pct >= 0
        emoji = "🔥" if is_surge else "💥"
        label = "급등" if is_surge else "급락"
        color = 0xFF8C00 if is_surge else 0x9400D3  # 주황 / 보라

        fields = [
            {
                "name": "📌 현재가",
                "value": f"`{current_price:,.2f}` KRW",
                "inline": True,
            },
            {
                "name": "📊 기준가 (마지막 확정 캔들)",
                "value": f"`{ref_price:,.2f}` KRW",
                "inline": True,
            },
            {
                "name": "📈 캔들 대비 변동",
                "value": f"**{change_pct:+.2%}**",
                "inline": True,
            },
        ]
        chg = _fmt_change(change_rate_24h)
        if chg:
            fields.append({
                "name": "📊 24h 등락",
                "value": f"`{chg}`",
                "inline": True,
            })

        embed = {
            "title": f"{emoji} [{label}] {_symbol_display(symbol)} {change_pct:+.1%} 이상 변동!",
            "color": color,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "fields": fields,
            "footer": {"text": "Spike Alert (15분봉 기준)"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_market_briefing(self, briefing: MarketBriefing) -> None:
        webhook_url = self._settings.discord_webhook_url
        if not webhook_url:
            return

        fields = []

        # \uac70\uc2dc\uacbd\uc81c \uc9c0\uc218 \uc139\uc158
        if briefing.indices:
            rows = "\n".join(_fmt_chg_row(q.name, q.price, q.change_pct) for q in briefing.indices)
            fields.append({
                "name": "\U0001f4ca \ubbf8\uad6d/\ud55c\uad6d \uc8fc\uc2dd \uc9c0\uc218",
                "value": rows,
                "inline": False,
            })

        # \uc8fc\ub3c4\uc8fc / \uc554\ud638\ud654\ud3d0
        if briefing.leaders:
            rows = "\n".join(_fmt_chg_row(q.name, q.price, q.change_pct) for q in briefing.leaders)
            fields.append({
                "name": "\U0001f4bc \uc8fc\ub3c4\uc8fc / \uc554\ud638\ud654\ud3d0",
                "value": rows,
                "inline": False,
            })

        # \uacf5\ud3ec\ud0d0\uc695
        if briefing.fear_greed is not None:
            fg = briefing.fear_greed
            fg_emoji = "\U0001f7e2" if fg >= 60 else ("\U0001f7e1" if fg >= 40 else "\U0001f534")
            fields.append({
                "name": "\U0001f9e0 \uacf5\ud3ec\ud0d0\uc695 \uc9c0\uc218",
                "value": f"{fg_emoji} `{fg}` — {briefing.fear_label or 'N/A'}",
                "inline": False,
            })

        if not fields:
            return

        embed = {
            "title": "\U0001f30f \uac70\uc2dc\uacbd\uc81c \ube0c\ub9ac\ud551",
            "color": 0x4169E1,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "fields": fields,
            "footer": {"text": "Scheduled Market Briefing"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_breaking_news(
        self, title: str, url: str, source: str
    ) -> None:
        webhook_url = self._settings.discord_webhook_url or \
                      self._settings.discord_signal_webhook_url
        if not webhook_url:
            return

        embed = {
            "title": f"\U0001f4e2 [{source}] {title[:200]}",
            "url": url,
            "color": 0xFF6B35,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "footer": {"text": "Breaking News"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_extreme(
        self, symbol: str, signal: ExtremeSignal, change_rate: float | None
    ) -> None:
        webhook_url = self._settings.discord_signal_webhook_url or \
                      self._settings.discord_webhook_url
        if not webhook_url:
            return

        color_map = {
            "panic_sell":  0xFF2222,
            "undervalued": 0x00CC66,
            "rebound":     0xFFAA00,
        }
        color = color_map.get(signal.type, 0x888888)

        reasons_text = "\n".join(f"• {r}" for r in signal.reasons) or "N/A"
        fields: list[dict] = [
            {
                "name": "\U0001f4cb \uac10\uc9c0 \uc774\uc720",
                "value": reasons_text,
                "inline": False,
            },
        ]
        if signal.values:
            val_text = " | ".join(f"{k}: `{v}`" for k, v in signal.values.items())
            fields.append({
                "name": "\U0001f4c8 \uc9c0\ud45c\uac12",
                "value": val_text[:512],
                "inline": False,
            })
        chg = _fmt_change(change_rate)
        if chg:
            fields.append({
                "name": "\U0001f4ca 24h \ub4f1\ub77d",
                "value": f"`{chg}`",
                "inline": True,
            })

        embed = {
            "title": f"{signal.emoji} [{signal.name}] {_symbol_display(symbol)}",
            "color": color,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "fields": fields,
            "footer": {"text": "Extreme Signal Alert"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_plain(self, content: str, webhook_url: str) -> None:
        if not webhook_url:
            return
        await self._discord_post(webhook_url, {"content": content[:2000]})

    async def _discord_post(self, webhook_url: str, payload: dict) -> None:
        """Discord 웹훅 POST.
        - 전역 Semaphore(1)로 직렬화 → burst 429 방지
        - 429 수신 시 Retry-After 헤더를 읽어 최대 2회 재시도
        """
        async with _DISCORD_SEMAPHORE:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    for attempt in range(3):
                        resp = await client.post(webhook_url, json=payload)
                        if resp.status_code == 429:
                            # Retry-After: 헤더(초 단위) 또는 JSON body retry_after(ms 또는 초)
                            retry_after: float = 1.0
                            try:
                                retry_after = float(
                                    resp.json().get("retry_after", 1.0)
                                )
                                # Discord는 retry_after를 초 단위로 반환
                            except Exception:
                                retry_after = float(
                                    resp.headers.get("Retry-After", "1")
                                )
                            logger.warning(
                                "discord.rate_limited",
                                retry_after=retry_after,
                                attempt=attempt,
                            )
                            await asyncio.sleep(retry_after + 0.1)
                            continue
                        resp.raise_for_status()
                        logger.debug("discord.sent")
                        return
            except Exception as exc:
                logger.warning("discord.send_failed", error=str(exc))

    # ── Telegram ──────────────────────────────────────────────────────

    async def _telegram_signal(
        self, decision: AIDecision, change_rate: float | None = None
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        sig = decision.trade_signal
        sig_val = sig.signal if isinstance(sig.signal, str) else sig.signal.value
        conf_val = sig.confidence if isinstance(sig.confidence, str) else sig.confidence.value
        emoji = _SIGNAL_EMOJI.get(SignalType(sig_val), "")

        risks = "\n".join(f"  • {r}" for r in sig.key_risks) if sig.key_risks else "  없음"
        chg = _fmt_change(change_rate)
        chg_line = f"24h 등락: `{chg}`\n" if chg else ""

        text = (
            f"{emoji} *{_symbol_display(decision.symbol)} — {sig_val}*\n\n"
            f"신뢰도: `{conf_val}` ({sig.confidence_score:.1f}/100)\n"
            f"현재가: `{sig.entry_price:,.6f}`\n"
            f"{chg_line}"
            f"손절: `{sig.stop_loss:,.6f}`\n"
            f"익절: `{sig.take_profit:,.6f}`\n"
            f"뉴스 영향: `{sig.news_impact}`\n\n"
            f"📝 *판단 근거*\n{sig.reasoning[:500]}\n\n"
            f"⚠️ *리스크*\n{risks}\n\n"
            f"_Model: {decision.model_version}_"
        )
        await self._telegram_plain(text)

    async def _telegram_breakout(
        self,
        symbol: str,
        triggered_conditions: list[dict],
        current_values: dict[str, float],
        change_rate: float | None = None,
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        def _fmt(v: float) -> str:
            return "N/A" if v != v else f"{v:.2f}"

        cond_lines = "\n".join(f"  • {c['name']}" for c in triggered_conditions)
        count = len(triggered_conditions)
        chg = _fmt_change(change_rate)
        chg_line = f"24h 등락: `{chg}`\n" if chg else ""

        text = (
            f"🚀 *[돌파 감지] {_symbol_display(symbol)} 기술적 조건 충족!*\n\n"
            f"{chg_line}"
            f"✅ *충족된 조건 ({count}/5)*\n{cond_lines}\n\n"
            f"📊 *전체 지표 현황*\n"
            f"RSI: `{_fmt(current_values.get('rsi', float('nan')))}` | "
            f"MACD: `{_fmt(current_values.get('macd', float('nan')))}`\n"
            f"StochK: `{_fmt(current_values.get('stoch_rsi_k', float('nan')))}` | "
            f"Vol: `{_fmt(current_values.get('volume_ratio', float('nan')))}x` | "
            f"ADX: `{_fmt(current_values.get('adx', float('nan')))}`"
        )
        await self._telegram_plain(text)

    async def _telegram_signal_brief(
        self, decision: AIDecision, change_rate: float | None = None
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        sig = decision.trade_signal
        sig_val = sig.signal if isinstance(sig.signal, str) else sig.signal.value
        conf_val = sig.confidence if isinstance(sig.confidence, str) else sig.confidence.value
        emoji = _SIGNAL_EMOJI.get(SignalType(sig_val), "")
        conf_emoji = _CONFIDENCE_EMOJI.get(conf_val, "")
        chg = _fmt_change(change_rate)
        chg_line = f"📊 24h 등락: `{chg}`\n" if chg else ""

        text = (
            f"₿ *{_symbol_display(decision.symbol)} — {sig_val}* (BTC 알림)\n\n"
            f"{conf_emoji} 신뢰도: `{conf_val}` ({sig.confidence_score:.1f}/100)\n"
            f"📌 현재가: `{sig.entry_price:,.2f}`\n"
            f"{chg_line}\n"
            f"📝 *판단 근거*\n{sig.reasoning[:400]}\n\n"
            f"_{emoji} {conf_val} | {decision.model_version}_"
        )
        await self._telegram_plain(text)

    async def _telegram_market_overview(
        self,
        gainers: list[MarketOverviewItem],
        losers: list[MarketOverviewItem],
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return
        if not gainers and not losers:
            return

        def _fmt_rows(items: list[MarketOverviewItem]) -> str:
            lines = []
            for i, item in enumerate(items, 1):
                name = _symbol_display(item.symbol)
                pct  = item.change_rate * 100
                sign = "▲" if pct >= 0 else "▼"
                lines.append(f"{i}. {name}  {sign} `{pct:+.2f}%`")
            return "\n".join(lines) if lines else "데이터 없음"

        parts = ["📊 *시장 등락률 현황 (24h)*\n"]
        if gainers:
            parts.append(f"🔥 *급등 TOP 5*\n{_fmt_rows(gainers)}")
        if losers:
            parts.append(f"\n💥 *급락 TOP 5*\n{_fmt_rows(losers)}")
        parts.append("\n_Upbit KRW 전 종목 기준_")

        await self._telegram_plain("\n".join(parts))

    async def _telegram_dominance(self, data: DominanceData) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        total_b = data.total_market_cap_usd / 1e9

        lines = [
            "🌐 *BTC 도미넌스 리포트*\n",
            f"₿ BTC 도미넌스: `{data.btc_dominance:.2f}%`",
        ]
        if data.eth_dominance > 0:
            lines.append(f"Ξ ETH 도미넌스: `{data.eth_dominance:.2f}%`")
        if total_b > 0:
            change_str = ""
            if data.market_cap_change_24h != 0:
                change_emoji = "📈" if data.market_cap_change_24h > 0 else "📉"
                change_str = f" ({change_emoji} {data.market_cap_change_24h:+.2f}% 24h)"
            lines.append(f"💰 전체 시총: `${total_b:,.1f}B`{change_str}")

        await self._telegram_plain("\n".join(lines))

    async def _telegram_spike(
        self,
        symbol: str,
        change_pct: float,
        current_price: float,
        ref_price: float,
        change_rate_24h: float | None = None,
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        emoji = "🔥" if change_pct >= 0 else "💥"
        label = "급등" if change_pct >= 0 else "급락"
        chg = _fmt_change(change_rate_24h)
        chg_line = f"24h 등락: `{chg}`\n" if chg else ""

        text = (
            f"{emoji} *[{label}] {_symbol_display(symbol)}*\n\n"
            f"캔들 대비 변동: `{change_pct:+.2%}`\n"
            f"{chg_line}"
            f"현재가: `{current_price:,.2f}` KRW\n"
            f"기준가: `{ref_price:,.2f}` KRW\n\n"
            f"_Spike Alert (15분봉 기준)_"
        )
        await self._telegram_plain(text)

    async def _telegram_market_briefing(self, briefing: MarketBriefing) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        parts = ["\U0001f30f *\uac70\uc2dc\uacbd\uc81c \ube0c\ub9ac\ud551*\n"]

        if briefing.indices:
            rows = "\n".join(_fmt_chg_row(q.name, q.price, q.change_pct) for q in briefing.indices)
            parts.append(f"\U0001f4ca *\uc8fc\uc2dd \uc9c0\uc218*\n{rows}")

        if briefing.leaders:
            rows = "\n".join(_fmt_chg_row(q.name, q.price, q.change_pct) for q in briefing.leaders)
            parts.append(f"\U0001f4bc *\uc8fc\ub3c4\uc8fc / \uc554\ud638\ud654\ud3d0*\n{rows}")

        if briefing.fear_greed is not None:
            fg = briefing.fear_greed
            fg_emoji = "\U0001f7e2" if fg >= 60 else ("\U0001f7e1" if fg >= 40 else "\U0001f534")
            parts.append(f"\U0001f9e0 *\uacf5\ud3ec\ud0d0\uc695*: `{fg}` {fg_emoji} {briefing.fear_label or ''}")

        if len(parts) <= 1:
            return

        await self._telegram_plain("\n\n".join(parts))

    async def _telegram_breaking_news(
        self, title: str, url: str, source: str
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        text = (
            f"\U0001f4e2 *[{source}] \uc18d\ubcf4*\n\n"
            f"{title[:400]}\n\n"
            f"[{source}\uc5d0\uc11c \ubcf4\uae30]({url})"
        )
        await self._telegram_plain(text)

    async def _telegram_extreme(
        self, symbol: str, signal: ExtremeSignal, change_rate: float | None
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        reasons_text = "\n".join(f"  • {r}" for r in signal.reasons) or "  N/A"
        chg = _fmt_change(change_rate)
        chg_line = f"24h \ub4f1\ub77d: `{chg}`\n" if chg else ""

        val_text = ""
        if signal.values:
            val_text = "\n\U0001f4c8 *\uc9c0\ud45c\uac12*\n" + " | ".join(
                f"{k}: `{v}`" for k, v in signal.values.items()
            )

        text = (
            f"{signal.emoji} *[{signal.name}] {_symbol_display(symbol)}*\n\n"
            f"\U0001f4cb *\uac10\uc9c0 \uc774\uc720*\n{reasons_text}\n\n"
            f"{chg_line}"
            f"{val_text}"
        )
        await self._telegram_plain(text)

    async def _telegram_plain(self, text: str) -> None:
        """Telegram sendMessage.
        - 전역 Semaphore(1)로 직렬화 → burst 429 방지
        - 429 수신 시 parameters.retry_after 읽어 최대 2회 재시도
        """
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        url = f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self._settings.telegram_chat_id,
            "text": text[:4096],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        async with _TELEGRAM_SEMAPHORE:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    for attempt in range(3):
                        resp = await client.post(url, json=payload)
                        if resp.status_code == 429:
                            retry_after: float = 1.0
                            try:
                                # Telegram: {"parameters": {"retry_after": N}}
                                retry_after = float(
                                    resp.json()
                                    .get("parameters", {})
                                    .get("retry_after", 1.0)
                                )
                            except Exception:
                                retry_after = float(
                                    resp.headers.get("Retry-After", "1")
                                )
                            logger.warning(
                                "telegram.rate_limited",
                                retry_after=retry_after,
                                attempt=attempt,
                            )
                            await asyncio.sleep(retry_after + 0.1)
                            continue
                        resp.raise_for_status()
                        logger.debug("telegram.sent")
                        return
            except Exception as exc:
                logger.warning("telegram.send_failed", error=str(exc))
