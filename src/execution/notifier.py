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

# Discord 웹훅은 웹훅 URL별로 30 req/min 제한이 있음.
# 동시 발송을 직렬화하여 burst 429를 방지한다.
_DISCORD_SEMAPHORE = asyncio.Semaphore(1)

# 업비트 KRW 마켓 심볼 → 한국어 코인명 매핑
_COIN_NAMES: dict[str, str] = {
    "KRW-BTC":   "비트코인",
    "KRW-ETH":   "이더리움",
    "KRW-XRP":   "리플",
    "KRW-SOL":   "솔라나",
    "KRW-DOGE":  "도지코인",
    "KRW-ADA":   "에이다",
    "KRW-AVAX":  "아발란체",
    "KRW-DOT":   "폴카닷",
    "KRW-LINK":  "체인링크",
    "KRW-TRX":   "트론",
    "KRW-SHIB":  "시바이누",
    "KRW-LTC":   "라이트코인",
    "KRW-BCH":   "비트코인캐시",
    "KRW-ETC":   "이더리움클래식",
    "KRW-NEAR":  "니어프로토콜",
    "KRW-ATOM":  "코스모스",
    "KRW-UNI":   "유니스왑",
    "KRW-ICP":   "인터넷컴퓨터",
    "KRW-APT":   "앱토스",
    "KRW-SUI":   "수이",
    "KRW-ARB":   "아비트럼",
    "KRW-OP":    "옵티미즘",
    "KRW-MATIC": "폴리곤",
    "KRW-FIL":   "파일코인",
    "KRW-SAND":  "더샌드박스",
    "KRW-MANA":  "디센트럴랜드",
    "KRW-AAVE":  "에이브",
    "KRW-VET":   "비체인",
    "KRW-XLM":   "스텔라루멘",
    "KRW-HBAR":  "헤데라",
    "KRW-ALGO":  "알고랜드",
    "KRW-STX":   "스택스",
    "KRW-GRT":   "더그래프",
    "KRW-FLOW":  "플로우",
    "KRW-CHZ":   "칠리즈",
    "KRW-SNX":   "신세틱스",
    "KRW-COMP":  "컴파운드",
    "KRW-MKR":   "메이커",
    "KRW-BAT":   "베이직어텐션토큰",
    "KRW-ICX":   "아이콘",
    "KRW-EOS":   "이오스",
    "KRW-QTUM":  "퀀텀",
    "KRW-XTZ":   "테조스",
    "KRW-IOTA":  "아이오타",
    "KRW-IOST":  "아이오에스티",
    "KRW-ONT":   "온톨로지",
    "KRW-ZIL":   "질리카",
    "KRW-STEEM": "스팀",
    "KRW-SC":    "시아코인",
    "KRW-LSK":   "리스크",
    "KRW-KAVA":  "카바",
    "KRW-MTL":   "메탈",
    "KRW-GLM":   "골렘",
    "KRW-SNT":   "스테이터스",
}


def _symbol_display(symbol: str) -> str:
    """'KRW-BTC' → 'KRW-BTC(비트코인)'  /  매핑 없으면 원본 반환"""
    name = _COIN_NAMES.get(symbol)
    return f"{symbol}({name})" if name else symbol

from config import get_settings
from src.ai.schemas import AIDecision, SignalType
from src.data.news_fetcher import DominanceData, MarketOverviewItem

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

    async def send_signal(self, decision: AIDecision) -> None:
        """매매 신호를 Discord + Telegram으로 동시 발송"""
        await asyncio.gather(
            self._discord_signal(decision),
            self._telegram_signal(decision),
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

    async def send_signal_brief(self, decision: AIDecision) -> None:
        """BTC 간단 알림 — HIGH가 아닌 신뢰도에서 현재가·신호·근거 중심으로 발송"""
        await asyncio.gather(
            self._discord_signal_brief(decision),
            self._telegram_signal_brief(decision),
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
    ) -> None:
        """규칙 기반 돌파 알림을 Discord + Telegram으로 발송"""
        await asyncio.gather(
            self._discord_breakout(symbol, triggered_conditions, current_values),
            self._telegram_breakout(symbol, triggered_conditions, current_values),
            return_exceptions=True,
        )

    async def send_spike_alert(
        self,
        symbol: str,
        change_pct: float,
        current_price: float,
        ref_price: float,
    ) -> None:
        """급등/급락(±10% 이상) 알림을 Discord + Telegram으로 발송"""
        await asyncio.gather(
            self._discord_spike(symbol, change_pct, current_price, ref_price),
            self._telegram_spike(symbol, change_pct, current_price, ref_price),
            return_exceptions=True,
        )

    # ── Discord ───────────────────────────────────────────────────────

    async def _discord_breakout(
        self,
        symbol: str,
        triggered_conditions: list[dict],
        current_values: dict[str, float],
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

        embed = {
            "title": f"🚀 [돌파 감지] {_symbol_display(symbol)} 기술적 조건 충족!",
            "color": 0x00BFFF,  # 하늘색 — AI 신호(초록/빨강)와 구별
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "fields": [
                {
                    "name": f"✅ 충족된 조건 ({count}/5)",
                    "value": cond_lines or "없음",
                    "inline": False,
                },
                {
                    "name": "📊 전체 지표 현황",
                    "value": summary,
                    "inline": False,
                },
            ],
            "footer": {"text": "Rule-based Breakout Alert"},
        }
        await self._discord_post(webhook_url, {"embeds": [embed]})

    async def _discord_signal_brief(self, decision: AIDecision) -> None:
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

        embed = {
            "title": f"₿ {_symbol_display(decision.symbol)} — {sig_val} (BTC 알림)",
            "color": color,
            "timestamp": decision.timestamp,
            "fields": [
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
                {
                    "name": "📝 판단 근거",
                    "value": sig.reasoning[:512],
                    "inline": False,
                },
            ],
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

    async def _discord_signal(self, decision: AIDecision) -> None:
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

        embed = {
            "title": f"{emoji} {_symbol_display(decision.symbol)} — {sig_val}",
            "color": color,
            "timestamp": decision.timestamp,
            "fields": [
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
            ],
            "footer": {"text": f"Model: {decision.model_version}"},
        }

        payload = {"embeds": [embed]}
        await self._discord_post(webhook_url, payload)

    async def _discord_spike(
        self,
        symbol: str,
        change_pct: float,
        current_price: float,
        ref_price: float,
    ) -> None:
        webhook_url = self._settings.discord_signal_webhook_url or \
                      self._settings.discord_webhook_url
        if not webhook_url:
            return

        is_surge = change_pct >= 0
        emoji = "🔥" if is_surge else "💥"
        label = "급등" if is_surge else "급락"
        color = 0xFF8C00 if is_surge else 0x9400D3  # 주황 / 보라

        embed = {
            "title": f"{emoji} [{label}] {_symbol_display(symbol)} {change_pct:+.1%} 이상 변동!",
            "color": color,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "fields": [
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
                    "name": "📈 변동률",
                    "value": f"**{change_pct:+.2%}**",
                    "inline": True,
                },
            ],
            "footer": {"text": "Spike Alert (15분봉 기준)"},
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

    async def _telegram_signal(self, decision: AIDecision) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        sig = decision.trade_signal
        sig_val = sig.signal if isinstance(sig.signal, str) else sig.signal.value
        conf_val = sig.confidence if isinstance(sig.confidence, str) else sig.confidence.value
        emoji = _SIGNAL_EMOJI.get(SignalType(sig_val), "")

        risks = "\n".join(f"  • {r}" for r in sig.key_risks) if sig.key_risks else "  없음"

        text = (
            f"{emoji} *{_symbol_display(decision.symbol)} — {sig_val}*\n\n"
            f"신뢰도: `{conf_val}` ({sig.confidence_score:.1f}/100)\n"
            f"현재가: `{sig.entry_price:,.6f}`\n"
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
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        def _fmt(v: float) -> str:
            return "N/A" if v != v else f"{v:.2f}"

        cond_lines = "\n".join(f"  • {c['name']}" for c in triggered_conditions)
        count = len(triggered_conditions)

        text = (
            f"🚀 *[돌파 감지] {_symbol_display(symbol)} 기술적 조건 충족!*\n\n"
            f"✅ *충족된 조건 ({count}/5)*\n{cond_lines}\n\n"
            f"📊 *전체 지표 현황*\n"
            f"RSI: `{_fmt(current_values.get('rsi', float('nan')))}` | "
            f"MACD: `{_fmt(current_values.get('macd', float('nan')))}`\n"
            f"StochK: `{_fmt(current_values.get('stoch_rsi_k', float('nan')))}` | "
            f"Vol: `{_fmt(current_values.get('volume_ratio', float('nan')))}x` | "
            f"ADX: `{_fmt(current_values.get('adx', float('nan')))}`"
        )
        await self._telegram_plain(text)

    async def _telegram_signal_brief(self, decision: AIDecision) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        sig = decision.trade_signal
        sig_val = sig.signal if isinstance(sig.signal, str) else sig.signal.value
        conf_val = sig.confidence if isinstance(sig.confidence, str) else sig.confidence.value
        emoji = _SIGNAL_EMOJI.get(SignalType(sig_val), "")
        conf_emoji = _CONFIDENCE_EMOJI.get(conf_val, "")

        text = (
            f"₿ *{_symbol_display(decision.symbol)} — {sig_val}* (BTC 알림)\n\n"
            f"{conf_emoji} 신뢰도: `{conf_val}` ({sig.confidence_score:.1f}/100)\n"
            f"📌 현재가: `{sig.entry_price:,.2f}`\n\n"
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
    ) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        emoji = "🔥" if change_pct >= 0 else "💥"
        label = "급등" if change_pct >= 0 else "급락"

        text = (
            f"{emoji} *[{label}] {_symbol_display(symbol)}*\n\n"
            f"변동률: `{change_pct:+.2%}`\n"
            f"현재가: `{current_price:,.2f}` KRW\n"
            f"기준가: `{ref_price:,.2f}` KRW\n\n"
            f"_Spike Alert (15분봉 기준)_"
        )
        await self._telegram_plain(text)

    async def _telegram_plain(self, text: str) -> None:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return

        url = f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self._settings.telegram_chat_id,
            "text": text[:4096],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                logger.debug("telegram.sent")
        except Exception as exc:
            logger.warning("telegram.send_failed", error=str(exc))
