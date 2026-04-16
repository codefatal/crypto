"""
Tests for news_fetcher.py — DominanceData, fetch_global_rss_news, NewsContext.global_items
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.news_fetcher import (
    DominanceData,
    FearGreedData,
    NewsContext,
    NewsItem,
    _fetch_rss_items,
    fetch_btc_dominance,
    fetch_global_rss_news,
)


# ── DominanceData ────────────────────────────────────────────────────────────

class TestDominanceData:
    def test_fields(self):
        d = DominanceData(
            btc_dominance=52.3,
            eth_dominance=17.1,
            total_market_cap_usd=2_500_000_000_000,
            market_cap_change_24h=1.5,
            updated_at="2026-01-01T00:00:00+00:00",
        )
        assert d.btc_dominance == 52.3
        assert d.eth_dominance == 17.1
        assert d.market_cap_change_24h == 1.5

    def test_unknown(self):
        d = DominanceData.unknown()
        assert d.btc_dominance == 0.0
        assert d.eth_dominance == 0.0
        assert d.total_market_cap_usd == 0.0


# ── NewsContext.global_items ──────────────────────────────────────────────────

class TestNewsContextGlobalItems:
    def _make_item(self, title: str, url: str, source: str = "coindesk") -> NewsItem:
        return NewsItem(
            id=f"id_{title}",
            title=title,
            url=url,
            source=source,
            published_at=datetime.now(tz=timezone.utc),
            sentiment=0,
        )

    def test_empty_has_global_items_field(self):
        ctx = NewsContext.empty()
        assert ctx.global_items == []

    def test_global_items_passed_through_for_coin(self):
        items = [self._make_item("BTC news", "https://example.com/btc")]
        ctx = NewsContext(
            naver_items=[],
            global_headlines="- [COINDESK] BTC news",
            fear_greed=FearGreedData.unknown(),
            global_items=items,
        )
        filtered = ctx.for_coin("BTC")
        assert filtered.global_items == items

    def test_for_coin_does_not_filter_global_items(self):
        """글로벌 뉴스는 코인 필터링 없이 그대로 전달"""
        items = [
            self._make_item("BTC news", "https://example.com/btc"),
            self._make_item("ETH news", "https://example.com/eth"),
        ]
        ctx = NewsContext(
            naver_items=[],
            global_headlines="",
            fear_greed=FearGreedData.unknown(),
            global_items=items,
        )
        assert len(ctx.for_coin("BTC").global_items) == 2
        assert len(ctx.for_coin("ETH").global_items) == 2

    def test_to_ai_context_uses_headlines_not_urls(self):
        """AI 프롬프트에는 URL이 없어야 한다 (토큰 절약)"""
        items = [self._make_item("Big news", "https://example.com/big")]
        ctx = NewsContext(
            naver_items=[],
            global_headlines="- [COINDESK] Big news",
            fear_greed=FearGreedData.unknown(),
            global_items=items,
        )
        ai_text = ctx.to_ai_context()
        assert "https://" not in ai_text
        assert "Big news" in ai_text


# ── fetch_btc_dominance ───────────────────────────────────────────────────────

class TestFetchBtcDominance:
    @pytest.mark.asyncio
    async def test_success_via_alternative_me(self):
        """alternative.me primary 소스로 정상 수신"""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "bitcoin_percentage_of_market_cap": 0.523,   # fraction → 52.3%
                "quotes": {"USD": {"total_market_cap": 2_500_000_000_000.0}},
            }
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("src.data.news_fetcher.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_btc_dominance()

        assert result.btc_dominance == 52.3
        assert result.total_market_cap_usd == 2_500_000_000_000.0
        # alternative.me는 ETH 제공 안 함
        assert result.eth_dominance == 0.0

    @pytest.mark.asyncio
    async def test_alternative_me_zero_falls_through_to_coingecko(self):
        """alternative.me가 btc=0 반환 시 CoinGecko fallback 사용"""
        alt_resp = MagicMock()
        alt_resp.raise_for_status = MagicMock()
        alt_resp.json.return_value = {
            "data": {
                "bitcoin_percentage_of_market_cap": 0.0,
                "quotes": {"USD": {"total_market_cap": 0.0}},
            }
        }

        gecko_resp = MagicMock()
        gecko_resp.raise_for_status = MagicMock()
        gecko_resp.json.return_value = {
            "data": {
                "market_cap_percentage": {"btc": 55.0, "eth": 18.0},
                "total_market_cap": {"usd": 2_600_000_000_000.0},
                "market_cap_change_percentage_24h_usd": 2.0,
            }
        }

        class FakeClient:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kwargs):
                if "alternative.me" in url:
                    return alt_resp
                if "coinpaprika" in url:
                    raise Exception("coinpaprika unavailable")
                if "coingecko" in url:
                    return gecko_resp
                raise Exception(f"unexpected url: {url}")

        with patch("src.data.news_fetcher.httpx.AsyncClient", FakeClient):
            result = await fetch_btc_dominance()

        assert result.btc_dominance == 55.0
        assert result.eth_dominance == 18.0

    @pytest.mark.asyncio
    async def test_returns_unknown_when_all_sources_fail(self):
        """모든 소스 실패 시 DominanceData.unknown() 반환"""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=Exception("network error"))

        with patch("src.data.news_fetcher.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_btc_dominance()

        assert result.btc_dominance == 0.0
        assert result.eth_dominance == 0.0


# ── fetch_global_rss_news ─────────────────────────────────────────────────────

class TestFetchGlobalRssNews:
    @pytest.mark.asyncio
    async def test_returns_tuple(self):
        """fetch_global_rss_news는 (str, list[NewsItem]) 튜플을 반환해야 한다"""
        with patch("src.data.news_fetcher._fetch_rss_items", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = []
            result = await fetch_global_rss_news()

        assert isinstance(result, tuple)
        assert len(result) == 2
        headline_text, items = result
        assert isinstance(headline_text, str)
        assert isinstance(items, list)

    @pytest.mark.asyncio
    async def test_headlines_built_from_items(self):
        item = NewsItem(
            id="abc",
            title="Test Title",
            url="https://example.com/test",
            source="coindesk",
            published_at=datetime.now(tz=timezone.utc),
            sentiment=0,
        )

        # RSS 소스가 2개(coindesk, decrypt)이므로 첫 소스만 결과 반환, 두 번째는 빈 리스트
        with patch("src.data.news_fetcher._fetch_rss_items", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = [[item], []]
            headline_text, items = await fetch_global_rss_news()

        assert "Test Title" in headline_text
        assert len(items) == 1
        assert items[0].url == "https://example.com/test"
        # 헤드라인 텍스트에는 URL 없음
        assert "https://" not in headline_text
