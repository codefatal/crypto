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

    # ── Discord ───────────────────────────────────────────────────────

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
            "title": f"{emoji} {decision.symbol} — {sig_val}",
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

    async def _discord_plain(self, content: str, webhook_url: str) -> None:
        if not webhook_url:
            return
        await self._discord_post(webhook_url, {"content": content[:2000]})

    async def _discord_post(self, webhook_url: str, payload: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
                logger.debug("discord.sent")
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
            f"{emoji} *{decision.symbol} — {sig_val}*\n\n"
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
