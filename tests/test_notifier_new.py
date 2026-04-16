"""
Tests for new notifier methods — send_signal_brief, send_dominance
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.news_fetcher import DominanceData
from src.execution.notifier import Notifier


def _make_decision(signal="LONG", confidence="MEDIUM", score=65.0):
    """테스트용 AIDecision mock 생성"""
    sig = MagicMock()
    sig.signal = signal
    sig.confidence = confidence
    sig.confidence_score = score
    sig.entry_price = 145_000_000.0
    sig.stop_loss = 143_000_000.0
    sig.take_profit = 149_000_000.0
    sig.reasoning = "Supertrend 강세, EMA 정배열, RSI 55 중립 구간. 단기 모멘텀 유효."
    sig.key_risks = ["거시경제 불확실성"]
    sig.news_impact = "POSITIVE"
    sig.indicator_summary = {"supertrend": "bull"}

    decision = MagicMock()
    decision.symbol = "KRW-BTC"
    decision.trade_signal = sig
    decision.timestamp = datetime.now(tz=timezone.utc).isoformat()
    decision.model_version = "llama-3.3-70b-versatile"
    decision.analysis_duration_ms = 1234
    decision.is_fallback = False
    return decision


class TestSendSignalBrief:
    @pytest.mark.asyncio
    async def test_calls_discord_and_telegram(self):
        notifier = Notifier()
        decision = _make_decision(confidence="MEDIUM")

        with patch.object(notifier, "_discord_signal_brief", new_callable=AsyncMock) as d_mock, \
             patch.object(notifier, "_telegram_signal_brief", new_callable=AsyncMock) as t_mock:
            await notifier.send_signal_brief(decision)

        d_mock.assert_called_once_with(decision)
        t_mock.assert_called_once_with(decision)

    @pytest.mark.asyncio
    async def test_discord_brief_skips_when_no_webhook(self):
        """웹훅 미설정 시 POST 요청 없음"""
        notifier = Notifier()
        notifier._settings = MagicMock()
        notifier._settings.discord_signal_webhook_url = ""
        notifier._settings.discord_webhook_url = ""

        with patch("src.execution.notifier.httpx.AsyncClient") as mock_client_cls:
            await notifier._discord_signal_brief(_make_decision())

        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_discord_brief_sends_embed(self):
        notifier = Notifier()
        notifier._settings = MagicMock()
        notifier._settings.discord_signal_webhook_url = "https://discord.com/api/webhooks/test"
        notifier._settings.discord_webhook_url = ""

        posted_payload = {}

        async def fake_post(url, json):
            posted_payload.update(json)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        with patch("src.execution.notifier.httpx.AsyncClient", return_value=mock_client):
            await notifier._discord_signal_brief(_make_decision(confidence="MEDIUM"))

        assert "embeds" in posted_payload
        embed = posted_payload["embeds"][0]
        assert "BTC 알림" in embed["title"]
        field_names = [f["name"] for f in embed["fields"]]
        assert any("신뢰도" in n for n in field_names)
        assert any("현재가" in n for n in field_names)


class TestSendDominance:
    @pytest.mark.asyncio
    async def test_calls_discord_and_telegram(self):
        notifier = Notifier()
        data = DominanceData(
            btc_dominance=52.3,
            eth_dominance=17.1,
            total_market_cap_usd=2.5e12,
            market_cap_change_24h=1.5,
        )

        with patch.object(notifier, "_discord_dominance", new_callable=AsyncMock) as d_mock, \
             patch.object(notifier, "_telegram_dominance", new_callable=AsyncMock) as t_mock:
            await notifier.send_dominance(data)

        d_mock.assert_called_once_with(data)
        t_mock.assert_called_once_with(data)

    @pytest.mark.asyncio
    async def test_discord_dominance_embed_contains_values(self):
        notifier = Notifier()
        notifier._settings = MagicMock()
        notifier._settings.discord_webhook_url = "https://discord.com/api/webhooks/test"

        posted_payload = {}

        async def fake_post(url, json):
            posted_payload.update(json)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        data = DominanceData(
            btc_dominance=52.3,
            eth_dominance=17.1,
            total_market_cap_usd=2.5e12,
            market_cap_change_24h=-0.5,
        )

        with patch("src.execution.notifier.httpx.AsyncClient", return_value=mock_client):
            await notifier._discord_dominance(data)

        embed = posted_payload["embeds"][0]
        assert "도미넌스" in embed["title"]
        all_values = " ".join(f["value"] for f in embed["fields"])
        assert "52.3" in all_values
        assert "17.1" in all_values
