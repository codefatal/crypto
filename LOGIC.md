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
    │       └─ 5개 중 3개 이상 충족 → 즉시 알림 발송
    │
    └─ [C] AIAnalyzer.analyze()        → Groq LLM 호출
            └─ confidence = HIGH → 전체 알림 + 실거래 (HIGH만)

──────────────────────────────────────────────────────────

인터벌 루프 (AI·REST 독립, 캐시된 15분봉 사용)

    [D] _breakout_interval_loop()  — 5분마다
            └─ 캐시된 15분봉 기준 check_breakout_signals()
               3개 이상 충족 + cooldown(15분) 통과 → 알림 발송

    [E] _spike_check_loop()        — 1분마다
            └─ live_price vs 마지막 확정 캔들 종가 비교
               ±10% 이상 + cooldown(60분) 통과 → 급등락 알림 발송

    [F] _market_overview_loop()    — 시작 즉시 + 매시간 정각
            └─ 업비트 전 종목 24h 등락률 조회
               급등 TOP 5 / 급락 TOP 5 알림 발송
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

## [B/D] 규칙 기반 돌파 알림 — 5개 중 3개 이상 OR 조건

| # | 지표 | 조건 | 임계값 | 알림 예시 |
|---|---|---|---|---|
| 1 | RSI (14) | > | 50 | `RSI 50 돌파 (현재 55.2)` |
| 2 | MACD (12/26/9) | > | 0 | `MACD 0선 상향 (현재 0.0012)` |
| 3 | StochRSI K (14, smooth 3/3) | > | 50 | `StochRSI K 50 돌파 (현재 72.4)` |
| 4 | 거래량 비율 (현재 / MA20) | ≥ | 2.0배 | `거래량 폭등 (2.8배)` |
| 5 | ADX (14) | > | 20 | `ADX 20 돌파 (현재 24.1)` |

- **5개 중 3개 이상 충족 시에만 알림 발송** (`_BREAKOUT_MIN_CONDITIONS = 3`)
- NaN 값은 조건 미충족으로 처리
- 최소 캔들 수: 35개 미만이면 전체 스킵
- **[B]** 캔들 경계(15분봉 확정 시)마다 `asyncio.create_task`로 비블로킹 실행
- **[D]** 5분마다 인터벌 루프에서 캐시된 15분봉으로 추가 체크 (cooldown 15분)

---

## [C] AI 분석 결과 처리

| confidence | signal | 동작 |
|---|---|---|
| HIGH | LONG/SHORT | Discord/Telegram 전체 알림 + 실거래 실행 |
| MEDIUM / LOW | LONG/SHORT | 무시 (알림/거래 없음) |
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
| 돌파 알림 | 규칙 기반 3/5 이상 충족 | 하늘색 `#00BFFF` | Discord signal webhook + Telegram |
| 급등 알림 | 현재가 vs 기준가 +10% 이상 | 주황 `#FF8C00` | Discord signal webhook + Telegram |
| 급락 알림 | 현재가 vs 기준가 -10% 이상 | 보라 `#9400D3` | Discord signal webhook + Telegram |
| 시장 현황 TOP 5 | 시작 즉시 + 매시간 정각 | 인디고 `#5865F2` | Discord main webhook + Telegram |
| 도미넌스 리포트 | 시작 즉시 + 매시간 정각 | — | Discord main webhook + Telegram |
| 시스템 상태 | 시작 시 1회 | — | Discord main webhook + Telegram |
| 에러 알림 | 예외 발생 | — | Discord main webhook + Telegram |

> **AI 시작 알림**: 시작 시 BTC/ETH AI 분석에서 **HIGH만 알림 발송** — MEDIUM/LOW는 무시

---

## 코인 이름 표기

알림 메시지의 심볼은 `KRW-BTC(비트코인)` 형식으로 표기됩니다.
매핑이 없는 심볼은 `KRW-XXXX` 원본 그대로 표시됩니다.
매핑 목록: `src/execution/notifier.py` → `_COIN_NAMES` 딕셔너리 (50개+ 주요 코인)

---

## 인터벌 루프 요약

| 루프 | 주기 | 데이터 소스 | cooldown | 추가 REST |
|---|---|---|---|---|
| `_breakout_interval_loop` | 5분 | 캐시된 15분봉 DataFrame | 심볼별 15분 | 없음 |
| `_spike_check_loop` | 1분 | WS live_price + 캐시 종가 | 심볼별 60분 | 없음 |
| `_market_overview_loop` | 시작 즉시 + 매시간 | Upbit REST /v1/ticker | 없음 | 1회/시간 |
