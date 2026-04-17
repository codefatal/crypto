# LOGIC.md — AutoCrypto 매매 로직 상세 설명

## 전체 파이프라인 흐름

```
캔들 확정 (15분봉 경계)
    │
    ├─ [A] BakktaIndicator.compute()   → Score 0~100 계산
    │       │
    │       ├─ Score < 70 또는 NEUTRAL → 종료 (AI 미호출)
    │       │
    │       └─ Score ≥ 70 + LONG/SHORT → AI 분석 진행
    │
    ├─ [B] check_breakout_signals()    → 독립 실행 (비블로킹, 캔들 경계마다)
    │       └─ 5개 조건 모두 충족 → 즉시 알림 발송
    │
    ├─ [G] detect_market_extremes()    → 독립 실행 (비블로킹, 캔들 경계마다)
    │       └─ 패닉셀/저평가/반등 감지 → 즉시 알림 발송 (cooldown 60분)
    │
    └─ [C] AIAnalyzer.analyze()        → Groq LLM 호출
            └─ confidence = HIGH → 전체 알림 + 실거래 (HIGH만)

──────────────────────────────────────────────────────────

인터벌 루프 (AI·REST 독립, 캐시된 15분봉 사용)

    [D] _breakout_interval_loop()  — 5분마다
            └─ 캐시된 15분봉 기준 check_breakout_signals()
               5개 조건 모두 충족 + cooldown(15분) 통과 → 알림 발송

    [E] _spike_check_loop()        — 1분마다
            └─ live_price vs 마지막 확정 캔들 종가 비교
               ±10% 이상 + cooldown(60분) 통과 → 급등락 알림 발송

    [F] _market_overview_loop()    — 시작 즉시 + 매시간 정각
            └─ 업비트 전 종목 24h 등락률 조회
               급등 TOP 5 / 급락 TOP 5 알림 발송

    [H] _notice_monitor_loop()     — 5분마다
            └─ 업비트 공지 API 폴링 → 신규 공지 속보 발송
               BTC USD 라운드피겨 레벨 돌파 감지 → 속보 발송

스케줄러 (APScheduler AsyncIOScheduler, KST 기준)

    [I] KST 09:00 / 22:30  → fetch_market_briefing() → send_market_briefing()
            └─ 미국/한국 지수, NVDA/AAPL/삼성/BTC/ETH 시세 + 공포탐욕 지수
```

---

## [A] Bakkta 지표 — 점수 산출 방식

| 구성요소 | 배점 | LONG 조건 | SHORT 조건 |
|---|---|---|---|
| **Supertrend** (ATR 10, ×3.0) | 30점 | 종가 > 상단밴드 (상승 추세) | 종가 < 하단밴드 (하락 추세) |
| **EMA Ribbon** (5/8/13/21/34/55) | 25점 | 단기 EMA > 장기 EMA 순서 정렬 | 장기 EMA > 단기 EMA 역정렬 |
| **Squeeze Momentum** (LazyBear, 20) | 20점 | Squeeze 해제 + Momentum > 0 | Squeeze 해제 + Momentum < 0 |
| **RSI** (14) | 15점 | RSI < 35 (과매도) | RSI > 65 (과매수) |
| **Volume Spike** (MA20 × 1.5배) | 10점 | 우세 방향 강화 | 우세 방향 강화 |

### 최종 방향 결정

| 조건 | 방향 |
|---|---|
| LONG score > SHORT score AND LONG score ≥ 50 | LONG |
| SHORT score > LONG score AND SHORT score ≥ 50 | SHORT |
| 그 외 | NEUTRAL |

### AI 게이트 (`AI_MIN_SCORE`, 기본 70)

| Score | 방향 | AI 호출 여부 |
|---|---|---|
| ≥ 70 | LONG 또는 SHORT | 호출 |
| < 70 | — | 미호출 (조기 종료) |
| — | NEUTRAL | 미호출 (조기 종료) |

---

## [G] 극단 신호 감지 — 패닉셀 / 저평가 / 반등

`detect_market_extremes(df)` → `list[ExtremeSignal]`

| 신호 | 조건 | 설명 |
|---|---|---|
| 🚨 패닉셀 | `volume ≥ vol_MA20 × 3` AND `(close-open)/open ≤ -3%` | 대량 매도 동반 급락 캔들 |
| 💡 저평가 | `RSI(14) ≤ 25` AND `close < BB_lower(20,2)` | 극단 과매도 + 볼린저 하단 이탈 |
| 🚀 반등 | `StochRSI_K ≤ 20` + K 골든크로스 + 양봉 + `MACD_hist 상승` | 4가지 조건 모두 충족 시 반등 신호 |

- **cooldown**: 동일 심볼+신호 타입은 60분 이내 재발송 억제
- NaN·데이터 부족 시 해당 신호 미발동

### ExtremeSignal 구조

```python
@dataclass
class ExtremeSignal:
    type: str        # "panic_sell" | "undervalued" | "rebound"
    emoji: str
    name: str
    reasons: list[str]   # 충족 이유 목록
    values: dict[str, Any]  # 로깅/알림용 지표값
```

---

## [B/D] 규칙 기반 돌파 알림 — 5개 조건 전부 AND 조건

| # | 지표 | 조건 | 임계값 | 알림 예시 |
|---|---|---|---|---|
| 1 | RSI (14) | > | 50 | `RSI 50 돌파 (현재 55.2)` |
| 2 | MACD (12/26/9) | > | 0 | `MACD 0선 상향 (현재 0.0012)` |
| 3 | StochRSI K (14, smooth 3/3) | > | 50 | `StochRSI K 50 돌파 (현재 72.4)` |
| 4 | 거래량 비율 (현재 / MA20) | ≥ | 2.0배 | `거래량 폭등 (2.8배)` |
| 5 | ADX (14) | > | 20 | `ADX 20 돌파 (현재 24.1)` |

- **5개 조건 모두 충족 시에만 알림 발송** (`_BREAKOUT_MIN_CONDITIONS = 5`)
- NaN 값은 조건 미충족으로 처리
- 최소 캔들 수: 35개 미만이면 전체 스킵
- **[B]** 캔들 경계(15분봉 확정 시)마다 `asyncio.create_task`로 비블로킹 실행
- **[D]** 5분마다 인터벌 루프에서 캐시된 15분봉으로 추가 체크 (cooldown 15분)

---

## [C] AI 분석 결과 처리

| confidence | signal | 동작 |
|---|---|---|
| HIGH | LONG/SHORT | Discord/Telegram 전체 알림 + 실거래 실행 |
| MEDIUM / LOW | LONG/SHORT | 무시 (알림/거래 없음) — BTC 포함 전 심볼 동일 |
| — | NEUTRAL | 무시 |
| — | — (fallback) | DB 기록만 (알림/거래 없음) |

### AI 안전장치

| 항목 | 기본값 | 설명 |
|---|---|---|
| 일일 토큰 한도 (`AI_DAILY_TOKEN_LIMIT`) | 90,000 | 초과 시 당일 AI 호출 전체 중단 (자정 UTC 리셋) |
| JSON 파싱 재시도 (`AI_MAX_RETRIES`) | 2회 | 실패 시 NEUTRAL fallback 반환 |
| 동시 호출 제한 | Semaphore(4) | TPM 초과 방지 |
| 429 응답 대기 | 자동 파싱 | `"6.9s"`, `"49m7.1s"` 포맷 모두 처리 |

---

## [E] 급등/급락 감지

| 항목 | 내용 |
|---|---|
| 기준 | 마지막 확정 15분봉 종가 (`df["close"].iloc[-1]`) |
| 현재가 | WS ticker `live_price` (REST 없음) |
| 임계값 | ±10% 이상 (`change_pct = (live - ref) / ref`) |
| 체크 주기 | 1분마다 |
| cooldown | 심볼별 60분 (60분 이내 동일 심볼 재발송 억제) |
| 급등 색상 | 주황 `#FF8C00` |
| 급락 색상 | 보라 `#9400D3` |

---

## 실거래 실행 조건

| 조건 | 통과 기준 |
|---|---|
| AI confidence | HIGH만 |
| AI signal | LONG 또는 SHORT (NEUTRAL 제외) |
| TRADE_ENABLED | `.env`에서 `true` 설정 필요 (기본 `false` = dry-run) |
| 포지션 한도 | `MAX_OPEN_POSITIONS` 미만일 때만 |
| 거래 금액 | 잔고 × `RISK_PER_TRADE_PCT` (기본 2%) |

---

## 알림 채널 구분

| 알림 종류 | 트리거 | 색상 | 채널 |
|---|---|---|---|
| 매매 신호 (전체) | AI HIGH | 초록(LONG) / 빨강(SHORT) | Discord signal webhook + Telegram |
| 돌파 알림 | 규칙 기반 5/5 모두 충족 | 하늘색 `#00BFFF` | Discord signal webhook + Telegram |
| 급등 알림 | 현재가 vs 기준가 +10% 이상 | 주황 `#FF8C00` | Discord signal webhook + Telegram |
| 급락 알림 | 현재가 vs 기준가 -10% 이상 | 보라 `#9400D3` | Discord signal webhook + Telegram |
| 극단 신호 (패닉셀) | 거래량×3 + 하락≥3% | 빨강 `#FF2222` | Discord signal webhook + Telegram |
| 극단 신호 (저평가) | RSI≤25 + BB하단 | 초록 `#00CC66` | Discord signal webhook + Telegram |
| 극단 신호 (반등) | StochRSI+골든+양봉+MACD | 주황 `#FFAA00` | Discord signal webhook + Telegram |
| 시장 현황 TOP 5 | 시작 즉시 + 매시간 정각 | 인디고 `#5865F2` | Discord main webhook + Telegram |
| 도미넌스 리포트 | 시작 즉시 + 매시간 정각 | — | Discord main webhook + Telegram |
| 거시경제 브리핑 | KST 09:00 / 22:30 | 파랑 `#4169E1` | Discord main webhook + Telegram |
| 업비트 공지 속보 | 신규 공지 감지 | 주황 `#FF6B35` | Discord main webhook + Telegram |
| BTC 라운드피겨 | 레벨 돌파 감지 | 주황 `#FF6B35` | Discord main webhook + Telegram |
| 시스템 상태 | 시작 시 1회 | — | Discord main webhook + Telegram |
| 에러 알림 | 예외 발생 | — | Discord main webhook + Telegram |

> **AI 알림 기준**: HIGH 신뢰도만 알림 발송 (시작 시 BTC/ETH 포함 전 심볼) — MEDIUM/LOW는 무시

---

## 코인 이름 표기

알림 메시지의 심볼은 `KRW-BTC(비트코인)` 형식으로 표기됩니다.
매핑이 없는 심볼은 `KRW-XXXX` 원본 그대로 표시됩니다.
매핑 목록: `src/execution/notifier.py` → `_COIN_NAMES` 딕셔너리 (약 50개 주요 코인)
- 모든 한글 값은 `\uXXXX` 유니코드 이스케이프로 저장 — Windows/Linux 인코딩 차이 무관

---

## 알림 Rate Limit 처리 (Discord / Telegram)

| 플랫폼 | 제한 | 대응 |
|---|---|---|
| Discord | 30 req/min per webhook | `_DISCORD_SEMAPHORE = asyncio.Semaphore(1)` 직렬화 + 429 시 `retry_after` JSON 파싱 후 대기 |
| Telegram | 1 msg/sec (동일 채팅) | `_TELEGRAM_SEMAPHORE = asyncio.Semaphore(1)` 직렬화 + 429 시 `parameters.retry_after` 파싱 후 대기 |

- 최대 3회 재시도 후 실패 시 로그만 남기고 계속 진행 (`return_exceptions=True`)

---

## 인터벌 루프 요약

| 루프 | 주기 | 데이터 소스 | cooldown | 추가 REST |
|---|---|---|---|---|
| `_breakout_interval_loop` | 5분 | 캐시된 15분봉 DataFrame | 심볼별 15분 | 없음 |
| `_spike_check_loop` | 1분 | WS live_price + 캐시 종가 | 심볼별 60분 | 없음 |
| `_market_overview_loop` | 시작 즉시 + 매시간 | Upbit REST /v1/ticker | 없음 | 1회/시간 |
| `_notice_monitor_loop` | 5분 | Upbit 공지 API + yfinance BTC-USD | 없음 | 1회/5분 |
| APScheduler (KST 09:00/22:30) | 하루 2회 | yfinance 지수/주도주 | 없음 | 1회/실행 |

## [H] 업비트 공지 + BTC 라운드피겨 감시 (`src/data/notice_monitor.py`)

### 공지 감시 (`NoticeMonitor.check_notices()`)

- Upbit `/v1/market/notice` API를 5분마다 폴링
- 키워드 필터: `NOTICE_KEYWORDS` 환경변수 쉼표 구분, 미설정 시 기본 목록 (`상장`, `신규`, `주의` 등)
- 중복 제거: `_seen_ids` set으로 이미 발송한 공지 재발송 방지
- **시작 시 false-positive 방지**: `_initialized = False` 상태에서 첫 번째 호출은 현재 공지를 스냅샷으로만 저장하고 빈 목록 반환

### BTC 라운드피겨 감지 (`NoticeMonitor.check_btc_round_figures()`)

- `fetch_btc_usd_price()`로 수집한 BTC-USD 가격 기준
- 레벨 목록: $30K~$200K 사이 주요 정수 (30K/35K/.../100K/110K/.../200K)
- `_last_btc_level_idx`와 현재 레벨 인덱스 비교 → 레벨 돌파 시 `RoundFigureAlert` 반환

## [I] 거시경제 브리핑 (`src/data/market_fetcher.py`)

yfinance `history(period="5d", interval="1d")` 사용 — 주말/장마감 날에도 안정적 동작.

| 분류 | 심볼 | 표시명 |
|---|---|---|
| 미국 지수 | `^GSPC`, `^IXIC`, `^DJI` | S&P 500, NASDAQ, DOW |
| 한국 지수 | `^KS11` | KOSPI |
| 주도주 | `NVDA`, `AAPL`, `005930.KS` | NVDA, AAPL, 삼성전자 |
| 암호화폐 | `BTC-USD`, `ETH-USD` | BTC, ETH |
| 공포탐욕 | alternative.me API | 0~100 + 레이블 |

`MarketBriefing` dataclass: `indices`, `leaders`, `fear_greed`, `fear_label`
