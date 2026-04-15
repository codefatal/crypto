"""
News Fetcher
────────────
세 가지 소스를 병렬로 수집하고 NewsContext로 통합 반환합니다.

  1. 네이버 뉴스 검색 API  — 코인별 한국어 최신 뉴스 (선택적)
  2. 글로벌 RSS 피드        — CoinTelegraph + CoinDesk (인증 불필요)
  3. 공포·탐욕 지수        — alternative.me API (인증 불필요)

CryptoPanic 의존성 완전 제거 — 회원가입이나 API 키 없이 동작합니다.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx
import structlog

from config import get_settings

logger = structlog.get_logger(__name__)

# ── RSS 피드 목록 (글로벌 뉴스 — 무료, 인증 불필요) ─────────────────────
# CoinTelegraph는 직접 접속 차단(SSL/연결 오류) → Decrypt로 대체
_GLOBAL_RSS_FEEDS: dict[str, str] = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "decrypt": "https://decrypt.co/feed",
}

_RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AutoCrypto RSS reader/1.0)",
}

_GLOBAL_RSS_PER_SOURCE = 5  # 소스당 최대 기사 수

# ── 공포·탐욕 지수 API ────────────────────────────────────────────────
_FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"

# ── 코인 심볼 → 한국어 검색 키워드 ─────────────────────────────────────
_KR_KEYWORD: dict[str, str] = {
    "BTC": "비트코인",
    "ETH": "이더리움",
    "XRP": "리플",
    "SOL": "솔라나",
    "BNB": "바이낸스코인",
    "ADA": "에이다 코인",
    "DOGE": "도지코인",
    "AVAX": "아발란체",
    "DOT": "폴카닷",
    "MATIC": "폴리곤",
    "LINK": "체인링크",
    "ATOM": "코스모스",
    "UNI": "유니스왑",
    "LTC": "라이트코인",
    "BCH": "비트코인캐시",
    "NEAR": "니어프로토콜",
    "ICP": "인터넷컴퓨터",
    "APT": "앱토스",
    "ARB": "아비트럼",
    "OP": "옵티미즘",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).strip()


# ── 데이터 클래스 ─────────────────────────────────────────────────────

@dataclass
class NewsItem:
    id: str
    title: str
    url: str
    source: str
    published_at: datetime
    sentiment: int  # +1=긍정, 0=중립, -1=부정
    currencies: list[str] = field(default_factory=list)
    summary: str = ""
    language: str = "en"  # "ko" for Korean news

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "sentiment": self.sentiment,
            "currencies": self.currencies,
            "summary": self.summary,
            "language": self.language,
        }


@dataclass
class FearGreedData:
    """alternative.me 공포·탐욕 지수"""
    score: int         # 0~100
    label: str         # "Extreme Fear" / "Fear" / "Neutral" / "Greed" / "Extreme Greed"
    updated_at: str = ""

    def to_text(self) -> str:
        return f"공포·탐욕 지수: {self.score}/100 ({self.label})"

    @classmethod
    def unknown(cls) -> "FearGreedData":
        return cls(score=-1, label="Unknown")


@dataclass
class NewsContext:
    """
    AI에 전달할 전체 뉴스 컨텍스트.

    naver_items   : 네이버 뉴스 (한국어, 코인별 필터링 가능)
    global_headlines : 글로벌 RSS 뉴스 제목 포맷 문자열
    fear_greed    : alternative.me 공포·탐욕 지수
    """
    naver_items: list[NewsItem]
    global_headlines: str         # fetch_global_rss_news() 결과
    fear_greed: FearGreedData

    def to_ai_context(self) -> str:
        """네이버 + 글로벌 RSS + 공포·탐욕 지수를 하나의 텍스트 블록으로 병합"""
        parts: list[str] = []

        # 1. 공포·탐욕 지수
        if self.fear_greed.score >= 0:
            parts.append(self.fear_greed.to_text())

        # 2. 네이버 뉴스 (한국어)
        if self.naver_items:
            naver_lines = "\n".join(
                f"- [KO] {item.title} ({item.source})"
                for item in self.naver_items[:10]
            )
            parts.append(f"## 한국어 뉴스\n{naver_lines}")

        # 3. 글로벌 RSS 뉴스
        if self.global_headlines:
            parts.append(f"## 글로벌 뉴스\n{self.global_headlines}")

        return "\n\n".join(parts) if parts else "관련 뉴스 없음"

    def for_coin(self, coin: str) -> "NewsContext":
        """특정 코인 관련 네이버 뉴스만 필터링한 새 NewsContext 반환.
        글로벌 뉴스와 공포·탐욕 지수는 모든 코인에 공통 적용."""
        filtered = [
            item for item in self.naver_items
            if not item.currencies or coin in item.currencies
        ]
        return NewsContext(
            naver_items=filtered,
            global_headlines=self.global_headlines,
            fear_greed=self.fear_greed,
        )

    @classmethod
    def empty(cls) -> "NewsContext":
        return cls(
            naver_items=[],
            global_headlines="",
            fear_greed=FearGreedData.unknown(),
        )


# ── 독립 비동기 함수 ──────────────────────────────────────────────────

async def fetch_global_rss_news() -> str:
    """
    CoinDesk + Decrypt RSS에서 각 5개 기사를 수집하여
    포맷된 텍스트 블록으로 반환합니다.

    인증/API 키 불필요. 실패 시 빈 문자열 반환.
    """
    lines: list[str] = []
    async with httpx.AsyncClient(
        timeout=10,
        follow_redirects=True,
        headers=_RSS_HEADERS,
    ) as client:
        tasks = [
            _fetch_rss_titles(client, name, url, _GLOBAL_RSS_PER_SOURCE)
            for name, url in _GLOBAL_RSS_FEEDS.items()
        ]
        batches = await asyncio.gather(*tasks, return_exceptions=True)

    for batch in batches:
        if isinstance(batch, list):
            lines.extend(batch)

    return "\n".join(lines)


async def _fetch_rss_titles(
    client: httpx.AsyncClient,
    source: str,
    url: str,
    limit: int,
) -> list[str]:
    """RSS 피드에서 기사 제목을 'limit'개 가져와 포맷된 문자열 목록으로 반환"""
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception as exc:
        logger.debug("rss.failed", source=source, error=str(exc))
        return []

    result: list[str] = []
    for entry in feed.entries[:limit]:
        title: str = entry.get("title", "").strip()
        if title:
            result.append(f"- [{source.upper()}] {title}")
    return result


async def fetch_fear_and_greed_index() -> FearGreedData:
    """
    alternative.me에서 현재 암호화폐 공포·탐욕 지수를 조회합니다.

    API 엔드포인트: https://api.alternative.me/fng/?limit=1
    인증/API 키 불필요. 실패 시 FearGreedData.unknown() 반환.
    """
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(_FEAR_GREED_URL)
            resp.raise_for_status()
            data = resp.json()

        entry = data["data"][0]
        score = int(entry["value"])
        label = entry.get("value_classification", "Unknown")
        updated_at = entry.get("timestamp", "")

        logger.info("fear_greed.fetched", score=score, label=label)
        return FearGreedData(score=score, label=label, updated_at=updated_at)

    except Exception as exc:
        logger.warning("fear_greed.failed", error=str(exc))
        return FearGreedData.unknown()


# ── NewsFetcher 클래스 ────────────────────────────────────────────────

class NewsFetcher:
    """
    복수 소스에서 암호화폐 뉴스를 수집합니다.

    Usage:
        fetcher = NewsFetcher()
        ctx = await fetcher.fetch_recent(symbols=["KRW-BTC", "KRW-ETH"])
        # ctx.to_ai_context() → AI 프롬프트용 텍스트
        # ctx.for_coin("BTC") → 코인 특화 필터링
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    async def fetch_recent(
        self,
        symbols: list[str] | None = None,
        max_age_seconds: int = 3600,
    ) -> NewsContext:
        """
        네이버 뉴스 + 글로벌 RSS + 공포·탐욕 지수를 병렬 수집하여
        NewsContext로 반환합니다.

        Args:
            symbols: 업비트 심볼 목록 (e.g. ["KRW-BTC", "KRW-ETH"]).
                     None이면 일반 암호화폐 뉴스를 반환.
            max_age_seconds: 네이버 뉴스 최대 허용 나이 (초).
        """
        coins: list[str] | None = None
        if symbols:
            coins = [_upbit_to_coin(s) for s in symbols]

        # 세 소스 병렬 수집
        naver_result, global_headlines, fear_greed = await asyncio.gather(
            self._fetch_naver(coins, max_age_seconds),
            fetch_global_rss_news(),
            fetch_fear_and_greed_index(),
            return_exceptions=False,
        )

        naver_items: list[NewsItem] = naver_result if isinstance(naver_result, list) else []

        logger.info(
            "news.fetched",
            naver=len(naver_items),
            global_headlines=len(global_headlines.splitlines()),
            fear_greed_score=fear_greed.score,
            symbols=coins,
        )

        return NewsContext(
            naver_items=naver_items,
            global_headlines=global_headlines,
            fear_greed=fear_greed,
        )

    # ── 소스: 네이버 뉴스 검색 API ───────────────────────────────────

    async def _fetch_naver(
        self,
        coins: list[str] | None,
        max_age_seconds: int,
    ) -> list[NewsItem]:
        """
        네이버 뉴스 검색 API로 코인 관련 한국어 뉴스를 수집합니다.
        키 미설정 시 빈 리스트 반환 (선택적 소스).
        """
        if not self._settings.naver_client_id or not self._settings.naver_client_secret:
            return []

        headers = {
            "X-Naver-Client-Id": self._settings.naver_client_id,
            "X-Naver-Client-Secret": self._settings.naver_client_secret,
        }

        queries: list[tuple[str, list[str]]] = []
        if coins:
            for coin in coins[:5]:
                keyword = _KR_KEYWORD.get(coin, coin)
                queries.append((f"{keyword} 코인", [coin]))
        else:
            queries.append(("암호화폐 코인", []))

        items: list[NewsItem] = []
        async with httpx.AsyncClient(timeout=10) as client:
            tasks = [
                self._naver_search(client, headers, query, related_coins)
                for query, related_coins in queries
            ]
            batches = await asyncio.gather(*tasks, return_exceptions=True)
            for batch in batches:
                if isinstance(batch, list):
                    items.extend(batch)

        # 시간 필터 + 중복 제거
        cutoff_ts = datetime.now(tz=timezone.utc).timestamp() - max_age_seconds
        seen: set[str] = set()
        fresh: list[NewsItem] = []
        for item in sorted(items, key=lambda x: x.published_at, reverse=True):
            if item.id in seen:
                continue
            if item.published_at.timestamp() < cutoff_ts:
                continue
            seen.add(item.id)
            fresh.append(item)

        return fresh

    async def _naver_search(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        query: str,
        currencies: list[str],
    ) -> list[NewsItem]:
        try:
            resp = await client.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params={
                    "query": query,
                    "display": 20,
                    "start": 1,
                    "sort": "date",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("naver.search_failed", query=query, error=str(exc))
            return []

        items: list[NewsItem] = []
        for article in data.get("items", []):
            item = _naver_to_item(article, currencies)
            if item:
                items.append(item)
        return items


# ── 내부 변환 헬퍼 ─────────────────────────────────────────────────────

def _upbit_to_coin(symbol: str) -> str:
    """업비트 심볼 → 코인 티커 ('KRW-BTC' → 'BTC')"""
    return symbol.replace("KRW-", "").upper()


def _naver_to_item(article: dict, currencies: list[str]) -> NewsItem | None:
    """네이버 뉴스 API 응답 → NewsItem 변환"""
    title_raw: str = article.get("title", "")
    link: str = article.get("link") or article.get("originallink", "")
    if not title_raw or not link:
        return None

    title = _strip_html(title_raw)
    description = _strip_html(article.get("description", ""))
    pub_str: str = article.get("pubDate", "")

    try:
        published_at = parsedate_to_datetime(pub_str).astimezone(timezone.utc)
    except Exception:
        published_at = datetime.now(tz=timezone.utc)

    item_id = hashlib.md5(link.encode()).hexdigest()
    return NewsItem(
        id=item_id,
        title=title,
        url=link,
        source="naver",
        published_at=published_at,
        sentiment=0,
        currencies=list(currencies),
        summary=description[:300],
        language="ko",
    )


def _parse_rss_date(entry: Any) -> datetime:
    """RSS 항목에서 게시 시각 파싱. 실패 시 현재 시각 반환."""
    pub = entry.get("published_parsed")
    if pub:
        try:
            return datetime(*pub[:6], tzinfo=timezone.utc)
        except Exception:
            pass

    pub_str: str = entry.get("published", "")
    if pub_str:
        try:
            return parsedate_to_datetime(pub_str).astimezone(timezone.utc)
        except Exception:
            pass

    return datetime.now(tz=timezone.utc)
