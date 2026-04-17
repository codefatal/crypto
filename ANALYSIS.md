# AutoCrypto AI 자동매매 시스템 — 기능 상세 분석

## 1. 시스템 전체 개요

암호화폐 거래소(업비트/바이낸스)의 **전 종목**을 실시간 감시하며, 캔들이 완성될 때마다 기술적 지표 계산 → AI 분석 → 자동 주문 실행까지 처리하는 풀파이프라인 봇입니다.

```
WebSocket(실시간 시세)
      │
      ▼ [캔들 경계 시각]
OHLCV 수집 (REST)
      │
      ├─ check_breakout_signals()   (비블로킹) → 5/5 충족 → send_breakout_alert()
      ├─ detect_market_extremes()   (비블로킹) → 패닉셀/저평가/반등 → send_extreme_alert()
      │
      ▼
BakktaIndicator (기술적 지표 계산)  ──── score < AI_MIN_SCORE(70) → 조기 종료
      │
      ▼
AIAnalyzer (Groq/Llama 3 AI 분석)
      │
      ├─ 폴백(NEUTRAL) → DB 기록만
      │
      ▼ [confidence=HIGH만]
Notifier (Discord + Telegram 알림)
      │
      ▼
Trader (시장가 주문 실행)
      │
      ▼
ReasoningLogger (SQLite/PostgreSQL 영구 저장)

[시작 즉시 + 매시간 정각] → fetch_btc_dominance() → send_dominance()
[시작 즉시 + 매시간 정각] → fetch_market_overview() → send_market_overview() (급등/급락 TOP 5)
[봇 시작 시] → _analyze_initial_coins() → BTC/ETH 즉시 분석 + HIGH만 알림
[5분마다] → _breakout_interval_loop() → check_breakout_signals() (5/5 충족 + cooldown 15분)
[1분마다] → _spike_check_loop() → ±10% 급등락 감지 (cooldown 60분)
[5분마다] → _notice_monitor_loop() → 업비트 공지 감시 + BTC 라운드피겨 감지
[KST 09:00 / 22:30] → APScheduler → fetch_market_briefing() → send_market_briefing()
```

---

## 2. 설정 시스템 (`config/settings.py`)

pydantic-settings 기반의 `.env` 파싱. `get_settings()`는 `lru_cache(1)`로 싱글턴처럼 동작 — 런타임 중 설정 변경 불가.

| 설정 키 | 기본값 | 역할 |
|---|---|---|
| `ACTIVE_EXCHANGE` | `upbit` | 거래소 선택 (upbit/binance) |
| `TIMEFRAME` | `minute15` | 캔들 타임프레임 |
| `TRADE_ENABLED` | `false` | false = DryRun 모드 |
| `MAX_OPEN_POSITIONS` | `5` | 최대 동시 포지션 수 |
| `RISK_PER_TRADE_PCT` | `0.02` | 포지션당 리스크 비율 (2%) |
| `MAX_POSITION_KRW` | `100,000` | 업비트 최대 포지션 KRW |
| `MAX_POSITION_USDT` | `100` | 바이낸스 최대 포지션 USDT |
| `MIN_VOLUME_KRW` | `1,000,000,000` | 최소 24h 거래대금 필터 |
| `AI_MODEL` | `llama-3.3-70b-versatile` | Groq 모델명 |
| `AI_MAX_RETRIES` | `2` | AI JSON 파싱 실패 재시도 횟수 |
| `AI_DAILY_TOKEN_LIMIT` | `90,000` | 일일 토큰 예산 상한 (Groq TPD 100k의 90%) |
| `AI_MIN_SCORE` | `70.0` | Bakkta 점수 최소값 — 이상일 때만 AI 호출 |
| `MAX_SYMBOLS_PER_CANDLE` | `30` | 캔들 경계당 AI 분석 대상 심볼 수 상한 |
| `SCAN_INTERVAL_SEC` | `60` | 뉴스 갱신 주기 (초) |

---

## 3. 데이터 수집 레이어

### 3-1. 업비트 스캐너 (`src/data/upbit_scanner.py`)

**심볼 수집**
- `pyupbit.get_tickers(fiat="KRW")`로 KRW 마켓 전 종목 수집
- 초기에는 전 종목 포함, ticker WebSocket 수신 후 `acc_trade_price_24h`(24h KRW 거래대금)로 동적 필터링

**초기 히스토리 로드**
- `Semaphore(8)` — 업비트 REST 초당 10건 한도 준수
- `pyupbit.get_ohlcv(symbol, interval, count=201)` → 마지막 행(현재 형성 중) 제거 → 200봉 보관

**실시간 가격 수신**
- `pyupbit.WebSocketManager("ticker", symbols)` — 내부적으로 `mp.Process` (멀티프로세스)
- 내부 큐가 name-mangled(`_WebSocketManager__q`)로 숨겨져 있어 직접 접근
- `asyncio.to_thread(_mp_q.get, True, 2.0)` — 2초 타임아웃으로 논블로킹 브릿지
- 수신 데이터: `trade_price`(현재가), `acc_trade_price_24h`(24h KRW 거래대금)

**캔들 경계 감지**
```
다음 캔들 경계 시각 = (현재시각 // 캔들초 + 1) × 캔들초 + 2초(버퍼)
```
- 15분봉이면 매 :00, :15, :30, :45분 정각 + 2초에 깨어남
- +2초 버퍼: 업비트 서버 캔들 확정 시간 여유

**캔들 발행**
- `MIN_VOLUME_KRW` 미만 심볼 건너뜀
- 통과 심볼을 24h KRW 거래대금 내림차순 정렬 후 **상위 `MAX_SYMBOLS_PER_CANDLE`개(기본 30개)만** 처리 (일일 토큰 예산 절약)
- `Semaphore(8)` 병렬 OHLCV 갱신 → `on_signal(symbol, df)` 콜백 호출

### 3-2. 바이낸스 스캐너 (`src/data/binance_scanner.py`)

- **심볼 수집**: `get_exchange_info()` + `get_ticker()` → USDT 마켓, TRADING 상태, `quoteVolume >= MIN_VOLUME_USDT` 필터 → 거래량 내림차순 정렬 후 상위 `MAX_SYMBOLS_PER_CANDLE`개로 제한
- **초기 로드**: `Semaphore(20)` — 바이낸스는 더 높은 rate limit 허용
- **실시간**: `BinanceSocketManager.multiplex_socket(streams)` — 25개 심볼씩 묶어 `{symbol}@kline_{tf}` 스트림 구독
- **캔들 완성 감지**: kline 메시지의 `k.x == true`일 때만 처리 (미완성 캔들 무시)
- **중복 방지**: `_last_close[symbol]`로 같은 close_time 재처리 방지

---

## 4. 기술적 지표 엔진 (`src/indicator/bakkta.py`)

PineScript의 "Bakkta Strategy"를 Python으로 포팅한 순수 numpy/pandas 모듈. I/O 없음.

### 5개 지표 계산 + 점수 집계

**① Supertrend (가중치 30점)**
- ATR(period=10) × 3.0 배수로 Upper/Lower Band 계산
- PineScript 로직 완전 포팅: 이전 캔들 종가 vs 밴드 비교로 방향 결정
- 마지막 봉 `direction_arr[-1] == 1` → 강세(LONG)

**② EMA Ribbon (가중치 25점)**
- 5/8/13/21/34/55 EMA 동시 계산
- 짧은 EMA > 긴 EMA 순서로 정렬(정배열) → 강세, 역배열 → 약세

**③ RSI (가중치 15점)**
- Wilder's smoothing (ewm com=13)
- RSI < 35 → LONG(과매도), RSI > 65 → SHORT(과매수), 나머지 → NEUTRAL

**④ 거래량 스파이크 (가중치 10점)**
- 현재 거래량 > 20봉 이동평균 × 1.5배 → 스파이크
- 방향 강화에만 사용 (단독 방향 결정 안 함)

**⑤ Squeeze Momentum — LazyBear 버전 (가중치 20점)**
- Bollinger Band가 Keltner Channel 안에 들어오면 squeeze(압축) 상태
- 이전 봉 squeeze + 현재 봉 해제 → `fired = True`
- momentum 값 > 0 → LONG, < 0 → SHORT

### 점수 집계 규칙

| 조건 | 결과 |
|---|---|
| long_score > short_score AND long_score ≥ 50 | LONG |
| short_score > long_score AND short_score ≥ 50 | SHORT |
| 나머지 | NEUTRAL |

**score < AI_MIN_SCORE(기본 70.0)이면 AI 호출 없이 조기 종료** (API 비용 절감)

### ATR 기반 SL/TP 계산

```
stop_loss_pct  = (ATR × 1.5) / close
take_profit_pct = stop_loss_pct × 2.0   # 2:1 RR
```

---

## 5. AI 분석 엔진 (`src/ai/analyzer.py`, `src/ai/schemas.py`)

### 프롬프트 전략
- Groq API (OpenAI 호환 엔드포인트)를 통해 Llama 3.3 70B 모델 사용
- JSON Schema를 시스템 프롬프트에 직접 삽입 (Groq/Llama는 Claude의 tool_use 미지원)
- temperature=0.1 — 낮은 창의성, 높은 JSON 형식 일관성

### AI에 전달되는 데이터
```
기술적 지표: 방향, 점수, Supertrend 강세 여부, EMA 정배열, RSI,
             거래량 스파이크, Squeeze 발동 여부 및 방향, ATR, 현재가
뉴스 컨텍스트: 공포·탐욕 지수, 한국어 뉴스(코인별 필터), 글로벌 RSS 뉴스
```

### AI 출력 스키마 (`TradeSignal`)

```json
{
  "signal": "LONG|SHORT|NEUTRAL",
  "confidence": "HIGH|MEDIUM|LOW",
  "confidence_score": 0~100,
  "entry_price": 현재가,
  "stop_loss": 손절가,
  "take_profit": 익절가,
  "reasoning": "판단 근거 (50자 이상, 최대 1000자)",
  "key_risks": ["리스크1", "리스크2"],
  "news_impact": "POSITIVE|NEGATIVE|NEUTRAL",
  "indicator_summary": {"supertrend": "bull", ...}
}
```

**Pydantic 유효성 검증**: LONG이면 `stop_loss < entry_price < take_profit`, SHORT이면 반대 방향 강제

### 3단계 JSON 안전장치

| 단계 | 처리 |
|---|---|
| 1단계 (Regex) | 마크다운 코드블록 제거, `{...}` 범위만 추출 |
| 2단계 (jsonschema + Pydantic) | 구조 검증 → 도메인 검증 (SL/TP 방향성 포함) |
| 3단계 (Fallback) | 2회 재시도 소진 후 NEUTRAL 폴백 반환 — 예외 전파 없음 |

**Retry 전략**: 실패 응답을 assistant 턴으로 추가 → 에러 내용을 user 턴으로 추가 → 모델이 자신의 이전 실수를 인지하고 수정하도록 유도

### Rate Limit(429) 처리

Groq에는 두 가지 독립적인 한도가 있습니다:

| 한도 | 내용 | 대응 |
|---|---|---|
| TPM (분당 토큰) | 분당 12,000 토큰 | `Semaphore(4)` 동시 호출 제한 |
| TPD (일일 토큰) | 하루 100,000 토큰 | 일일 예산 카운터 + 조기 차단 |

**retry-after 파싱 (`_parse_retry_after`)**
- `"try again in 49m7.104s"` → 2,949초 대기 (분+초 형식)
- `"try again in 6.915s"` → 7.9초 대기 (초 형식)
- 미매칭 시 기본값 60초

**일일 토큰 예산 관리**
- `_call_groq()` 응답에서 `response.usage.total_tokens` 실측값 누적
- 잔여량 < 4,000 토큰 시 `analyze()` → `None` 반환 + `ai.daily_budget_exhausted` 에러 로그
- 자정(UTC) 기준 자동 리셋 (`_next_midnight_utc()`)

---

## 6. 뉴스 수집 (`src/data/news_fetcher.py`)

4개 소스를 **병렬**(`asyncio.gather`)로 수집:

| 소스 | 인증 | 내용 |
|---|---|---|
| 네이버 뉴스 API | NAVER_CLIENT_ID/SECRET 필요 | 코인별 한국어 최신 뉴스 (쿼리당 20개) |
| CoinDesk RSS | 불필요 | 영문 글로벌 헤드라인 5개 (URL 포함 `NewsItem`) |
| Decrypt RSS | 불필요 | 영문 글로벌 헤드라인 5개 (URL 포함 `NewsItem`) |
| 공포·탐욕 지수 | 불필요 | alternative.me, 0~100 점수 + 레이블 |

**글로벌 RSS 이중 구조**: `fetch_global_rss_news()` → `tuple[str, list[NewsItem]]`
- `global_headlines (str)` — URL 없는 텍스트 (AI 프롬프트용, 토큰 절약)
- `global_items (list[NewsItem])` — URL 포함 (`[제목](url)` 마크다운으로 알림 발송)

**BTC 도미넌스 조회**: `fetch_btc_dominance()` — CoinGecko `/global` (무료, 인증 불필요)
- `DominanceData` dataclass: `btc_dominance`, `eth_dominance`, `total_market_cap_usd`, `market_cap_change_24h`
- `_dominance_check_loop()`에서 시작 즉시 1회 + 매시간 정각(+5초 버퍼)마다 호출

**시장 현황 조회**: `fetch_market_overview(top_n=5)` — Upbit REST `/v1/ticker` 전 종목 1회 호출
- `pyupbit.get_tickers(fiat="KRW")`로 심볼 목록 조회 후 일괄 요청
- `signed_change_rate` 기준 정렬 → 급등 TOP 5 / 급락 TOP 5 반환
- `MarketOverviewItem` dataclass: `symbol`, `change_rate`, `trade_price`
- `_market_overview_loop()`에서 시작 즉시 1회 + 매시간 정각(+10초 버퍼)마다 호출

**거시경제 브리핑**: `src/data/market_fetcher.py` — yfinance 기반 (Python 3.14 호환)
- `fetch_market_briefing()` → `MarketBriefing`: 미국/한국 지수 + 주도주/암호화폐 + 공포탐욕
- `fetch_btc_usd_price()` → BTC-USD 가격 (공지 모니터의 라운드피겨 감지용)
- yfinance `history(period="5d", interval="1d")` 사용 — 주말/장마감 안정성 확보
- KST 09:00 / 22:30 APScheduler에 의해 자동 실행

**업비트 공지 감시**: `src/data/notice_monitor.py` — `NoticeMonitor` 클래스
- `check_notices()`: Upbit 공지 API 폴링, 키워드 필터, 중복 제거, 시작 시 false-positive 방지
- `check_btc_round_figures(btc_usd)`: $30K~$200K 주요 레벨 돌파 감지 (`RoundFigureAlert`)
- 5분마다 `_notice_monitor_loop()`에서 호출

**코인별 필터링**: `NewsContext.for_coin("BTC")` — 네이버 뉴스만 코인별 필터링, 글로벌 RSS(`global_items` 포함)와 공포·탐욕 지수는 모든 코인 공통 적용

**뉴스 갱신 주기**: `SCAN_INTERVAL_SEC`(기본 60초)마다 백그라운드 태스크로 갱신

**최초 수집 시 다이제스트 발송**: 봇 시작 후 첫 번째 뉴스 수집 완료 즉시 Discord + Telegram으로 오늘 날짜 다이제스트 발송
- 공포·탐욕 지수 + 이모지
- 글로벌 헤드라인 (상위 8개, URL `[제목](url)` 형식)
- 한국어 뉴스 상위 5개 제목

---

## 7. 알림 시스템 (`src/execution/notifier.py`)

Discord + Telegram 동시 발송. `asyncio.gather(..., return_exceptions=True)` — 한 채널 실패가 다른 채널 차단 안 함.

### Discord Embed 매매 신호 형식

```
📈 KRW-BTC — LONG
🔥 신뢰도: HIGH (82.5/100) | 📌 현재가: `145,234,000` | 📊 24h 등락: `▲ +3.21%`
🔴 손절: `143,892,000`     | 🟢 익절: `147,890,000`
📰 뉴스 영향: POSITIVE     | ⏱️ 분석 소요: 1,234ms
📝 판단 근거: (최대 1024자)
⚠️ 주요 리스크: • 거시경제 불확실성 • ...
```

**24h 등락률 표기**: `_fmt_change(rate)` 헬퍼가 모든 알림에 공통 적용 (AI·규칙기반·급등락 모두)
- 소수 입력 (0.0321 = +3.21%) → `▲ +3.21%` / `▼ -2.10%`
- 스캐너 WebSocket 미수신 상태(시작 직후)이면 `None` → 해당 필드 생략

### 발송 시나리오

| 시나리오 | 트리거 | 포맷 |
|---|---|---|
| `send_signal()` | AI 신뢰도 HIGH인 매매 신호 | 전체 embed (신뢰도·현재가·손절·익절·근거·리스크 + 24h 등락) |
| `send_breakout_alert()` | 규칙 기반 5/5 조건 충족 | 하늘색 embed (충족 조건 목록 + 24h 등락) |
| `send_spike_alert()` | 현재가 vs 기준가 ±10% 이상 | 주황/보라 embed (캔들 대비 변동 + 24h 등락) |
| `send_extreme_alert()` | 패닉셀/저평가/반등 감지 | 색상 구분 embed (감지 이유, 지표값 + 24h 등락) |
| `send_market_briefing()` | KST 09:00 / 22:30 스케줄러 | 파랑 embed (지수·주도주 시세표 + 공포탐욕) |
| `send_breaking_news()` | 업비트 공지 / BTC 라운드피겨 | 주황 embed (제목 + URL 링크) |
| `send_market_overview()` | 시작 즉시 + 매시간 정각 | 인디고 embed (급등 TOP5 / 급락 TOP5, 등락률%) |
| `send_dominance()` | 시작 즉시 + 매시간 정각 | BTC/ETH 도미넌스 + 전체 시총 변화율 |
| `send_system_status()` | 봇 시작 / 종료 시 | 텍스트 |
| `send_news_digest()` | 봇 시작 후 첫 뉴스 수집 완료 시 | URL 포함 글로벌 뉴스 + 공포·탐욕 지수 |
| `send_error()` | 시스템 에러 발생 시 | 텍스트 |

### Discord / Telegram Rate Limit 처리

| 플랫폼 | Semaphore | 429 파싱 키 |
|---|---|---|
| Discord | `_DISCORD_SEMAPHORE = Semaphore(1)` | `resp.json()["retry_after"]` |
| Telegram | `_TELEGRAM_SEMAPHORE = Semaphore(1)` | `resp.json()["parameters"]["retry_after"]` |

최대 3회 재시도 후 실패 시 로그만 남기고 계속 진행.

### 코인 이름 표기

심볼은 `KRW-BTC(비트코인)` 형식으로 표기. `_COIN_NAMES` 딕셔너리(약 50개)에 `\uXXXX` 유니코드 이스케이프로 저장 — Windows/Linux 파일 인코딩 차이 무관. 매핑 없는 심볼은 원본 그대로 표시.

---

## 8. 주문 실행 레이어

### 업비트 트레이더 (`src/execution/trader.py`)

**포지션 관리**
- `_open_positions: dict[str, OrderResult]` — 메모리 내 포지션 추적 (재시작 시 초기화)
- 동일 심볼 재진입 차단 (`symbol in _open_positions`)
- `MAX_OPEN_POSITIONS` 초과 시 건너뜀

**투입 금액 계산**
```
amount = MAX_POSITION_KRW × RISK_PER_TRADE_PCT
       = 100,000 × 0.02 = 2,000 KRW  (기본값)
```

**DryRun vs 실거래**

| 모드 | 동작 |
|---|---|
| `TRADE_ENABLED=false` | `DRY-{symbol}-{timestamp}` ID 발급, 실제 주문 없음 |
| `TRADE_ENABLED=true` | pyupbit 시장가 주문 (매수: KRW 금액 기준, 매도: 코인 수량 기준) |

### 바이낸스 트레이더 (`src/execution/binance_trader.py`)

- python-binance `AsyncClient.create_order(type="MARKET")` 사용
- `tenacity.retry(stop_after_attempt(3))` — 주문 실패 시 3회 자동 재시도
- 체결 가격: `order["fills"][0]["price"]`에서 추출

---

## 9. 데이터베이스 레이어 (`src/execution/logger.py`)

SQLAlchemy 동기 ORM + NullPool. 모든 호출은 `asyncio.to_thread()`로 비동기 래핑.

> NullPool 사용 이유: 여러 심볼이 동시에 `log_indicator`를 호출할 때 StaticPool의 단일 커넥션 경쟁으로 FlushError 발생 → 스레드별 독립 연결 생성으로 해결

### 3개 테이블

**`ai_decisions`** — 모든 AI 판단 기록 (폴백 포함)

| 컬럼 | 내용 |
|---|---|
| signal, confidence, confidence_score | 매매 방향, 신뢰도 |
| entry_price, stop_loss, take_profit | 가격 정보 |
| reasoning | 판단 근거 텍스트 |
| key_risks, indicator_summary | JSON 배열/객체 |
| model_version, analysis_duration_ms | 모델 정보 |
| is_fallback, executed | 폴백 여부, 주문 실행 여부 |

**`indicator_snapshots`** — 캔들 경계마다 지표 스냅샷

| 컬럼 | 내용 |
|---|---|
| direction, score | 종합 방향과 점수 |
| supertrend_bull, ema_aligned_bull | 지표 불리언 |
| rsi, volume_spike, squeeze_fired | 지표 수치/불리언 |
| atr, close | ATR 값, 종가 |

**`trade_signals`** — 실행된 주문 기록

| 컬럼 | 내용 |
|---|---|
| decision_id | ai_decisions 참조 |
| side, quantity, entry_price | 주문 방향, 수량, 진입가 |
| stop_loss, take_profit | SL/TP 가격 |
| order_id, status | 거래소 주문 ID, 상태 |
| filled_price, pnl_value, closed_at | 체결 정보 (업데이트 미구현) |

**SQLite ↔ PostgreSQL 전환**: `DATABASE_URL`만 변경, 코드 변경 불필요

---

## 10. 메인 파이프라인 실행 흐름 (`main.py`)

### 시작 시퀀스

```
1. DB health_check()                → 실패 시 sys.exit(1)
2. trader.init()                    → 거래소 클라이언트 초기화
3. _log_btc_snapshot()              → BTC 현재가 + 최신 캔들 로그 출력 (파이프라인 검증)
4. _news_refresh_loop() 생성        → 백그라운드 뉴스 갱신 태스크
5. _dominance_check_loop() 생성     → BTC 도미넌스 즉시 1회 + 매시간 정각
6. send_system_status()             → Discord + Telegram 시작 알림
7. _analyze_initial_coins() 생성    → BTC/ETH 즉시 분석 + HIGH만 알림 (upbit 전용)
8. _breakout_interval_loop() 생성   → 5분마다 캐시 캔들 기반 돌파 체크
9. _spike_check_loop() 생성         → 1분마다 WS 현재가 기반 급등락 체크
10. _market_overview_loop() 생성    → 시작 즉시 + 매시간 정각 급등/급락 TOP 5
11. _notice_monitor_loop() 생성     → 5분마다 업비트 공지 + BTC 라운드피겨 감지
12. _start_scheduler()              → APScheduler KST 09:00/22:30 거시경제 브리핑 등록
13. scanner.start()                 → 블로킹 (무한 루프)
```

### 심볼 처리 파이프라인 (`_process_symbol`)

```
UpbitScanner._emit_all_candles()
  └─ 거래대금 상위 30개 심볼 (MAX_SYMBOLS_PER_CANDLE) → on_signal 호출

(비블로킹 태스크) check_breakout_signals(df)  → 5/5 충족 → send_breakout_alert()
(비블로킹 태스크) detect_market_extremes(df)  → 신호 감지 → send_extreme_alert() (cooldown 60분)

BakktaIndicator.compute(symbol, df)
  │
  ├─ result is None               → return (데이터 부족)
  ├─ score < AI_MIN_SCORE(70.0)   → return (약한 신호, AI 호출 없음)
  │
  └─ score ≥ 70.0
       ├─ DB: log_indicator (백그라운드 태스크)
       ├─ news_cache.for_coin(coin)
       └─ AIAnalyzer.analyze(symbol, result, news_ctx)
              │
              ├─ None (일일 예산 소진)  → return
              ├─ is_fallback=True       → DB: log_decision → return
              │
              └─ is_fallback=False
                   ├─ DB: log_decision → decision_id 획득
                   └─ confidence=HIGH
                        ├─ Notifier.send_signal(decision, change_rate)
                        └─ Trader.execute(decision) → OrderResult
                             └─ order 성공 시
                                  ├─ DB: log_trade
                                  └─ (실거래) DB: mark_decision_executed
```
> MEDIUM/LOW는 BTC 포함 전 심볼 모두 알림 없음
```

### 거래소 팩토리 (lazy import)

```python
def _build_scanner(on_signal):
    if exchange == "upbit":
        from src.data.upbit_scanner import UpbitScanner  # 여기서만 import
        return UpbitScanner(...)
    elif exchange == "binance":
        from src.data.binance_scanner import BinanceScanner  # 여기서만 import
        return BinanceScanner(...)
```

`ACTIVE_EXCHANGE=upbit`이면 `python-binance` 패키지가 **절대 import되지 않음** — websockets 버전 충돌(`websockets==12.0` vs Python 3.14) 방지

---

## 11. 동시성 아키텍처

```
asyncio 이벤트 루프 1개
  │
  ├─ _ws_consumer_loop()           WebSocket 메시지 수신 (2초 타임아웃)
  ├─ _candle_timer_loop()          캔들 경계 감지 + OHLCV 갱신
  ├─ _news_refresh_loop()          뉴스 60초 주기 갱신
  ├─ _dominance_check_loop()       BTC 도미넌스 시작 즉시 + 매시간 정각
  ├─ _breakout_interval_loop()     5분마다 캐시 캔들 기반 돌파 체크 (cooldown 15분)
  ├─ _spike_check_loop()           1분마다 WS 현재가 ±10% 급등락 체크 (cooldown 60분)
  ├─ _market_overview_loop()       시작 즉시 + 매시간 정각 급등/급락 TOP 5
  ├─ _notice_monitor_loop()        5분마다 업비트 공지 + BTC 라운드피겨 감지
  ├─ APScheduler (별도 스레드)      KST 09:00/22:30 거시경제 브리핑 전송
  │
  └─ _process_symbol()             심볼별 병렬 태스크 (asyncio.create_task)
       ├─ check_breakout_signals()  캔들마다 비블로킹 태스크
       ├─ detect_market_extremes()  캔들마다 비블로킹 태스크 (cooldown 60분)
       ├─ Semaphore(8)              OHLCV REST 병렬 호출 제한 (업비트 rate limit)
       ├─ Semaphore(4)              Groq API 동시 호출 제한 (TPM 보호)
       ├─ _DISCORD_SEMAPHORE(1)     Discord webhook 직렬화 (429 방지)
       ├─ _TELEGRAM_SEMAPHORE(1)    Telegram 직렬화 (429 방지)
       ├─ _daily_token_used         일일 사용 토큰 카운터 (응답 usage에서 실측)
       └─ asyncio.to_thread()       SQLite 동기 ORM 호출 (블로킹 작업 격리)
```

**`slow_callback_duration = 10.0`**: Python 3.14 기본 0.1초 임계값으로 인한 네트워크 I/O 경고 억제

---

## 12. VSCode 디버깅 구성

Python 3.14 + debugpy launch 모드 hang 문제 → attach 모드로 우회.

| F5 구성명 | 포트 | 타임프레임 |
|---|---|---|
| AutoCrypto — 15분봉 (기본) | 5678 | minute15 |
| AutoCrypto — 1분봉 | 5679 | minute1 |
| AutoCrypto — 5분봉 | 5680 | minute5 |
| AutoCrypto — 1시간봉 | 5681 | minute60 |

**동작 순서**: F5 → `preLaunchTask`가 `python -Xfrozen_modules=off main.py` 실행 → `DEBUGPY_PORT` 감지 → `debugpy.listen() + wait_for_client()` → VSCode attach

---

## 13. 현재 구현의 주요 한계점

| 항목 | 현재 상태 |
|---|---|
| 손절/익절 자동 실행 | SL/TP는 AI가 계산하지만 실제 모니터링/청산 로직 **미구현** |
| 포지션 PnL 추적 | `trade_signals.pnl_value` 컬럼 존재하나 업데이트 로직 **미구현** |
| 주문 체결 확인 | 시장가 주문 후 UUID만 저장, 체결 조회 **미구현** |
| SHORT 포지션 | 업비트는 현물 전용 → SELL 신호가 와도 보유 수량 없으면 의미 없음 |
| 메모리 포지션 관리 | `_open_positions`가 봇 재시작 시 초기화 (DB와 동기화 없음) |
