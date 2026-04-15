"""
AI Analyzer — Groq API (Llama 3) 버전
──────────────────────────────────────
오픈소스 LLM의 불안정한 JSON 출력을 3단계 안전장치로 제어합니다.

안전장치 1 — extract_json()
  정규표현식으로 응답에서 순수 { } 블록만 추출.
  마크다운 코드블록, 전후 텍스트 제거.

안전장치 2 — Retry 루프 (최대 AI_MAX_RETRIES회)
  JSONDecodeError / Pydantic ValidationError 발생 시
  실패 응답과 에러 내용을 대화 히스토리에 추가하여 재요청.
  "네가 보낸 JSON에 에러가 있으니 스키마에 맞게 다시 반환해" 패턴.

안전장치 3 — neutral_fallback()
  모든 재시도 소진 후에도 실패하면 NEUTRAL(관망) 신호 반환.
  절대 예외를 상위로 전파하지 않음 → 봇 전체 중단 방지.

API 구성:
  openai 패키지 + base_url="https://api.groq.com/openai/v1"
  모델: llama3-70b-8192 (기본값)
  temperature: 0.1 (낮을수록 JSON 형식 안정적)
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
from openai import AsyncOpenAI
from pydantic import ValidationError as PydanticValidationError

from config import get_settings
from src.ai.schemas import (
    TRADE_SIGNAL_JSON_SCHEMA,
    AIDecision,
    ConfidenceLevel,
    SignalType,
    TradeSignal,
    neutral_fallback,
)
from src.data.news_fetcher import NewsContext
from src.indicator.bakkta import BakktaResult

logger = structlog.get_logger(__name__)

# ── 시스템 프롬프트 ────────────────────────────────────────────────────
# JSON 스키마를 프롬프트에 직접 삽입하여 출력 형식을 강제합니다.
# (Groq/Llama는 Claude의 tool_use가 없으므로 이 방식이 가장 신뢰성 높음)

_SYSTEM_PROMPT_TEMPLATE = """당신은 암호화폐 기술적 분석 전문가입니다.

## 핵심 규칙 (반드시 준수)
1. 응답은 오직 순수한 JSON 객체 하나만 반환하세요.
2. 마크다운 코드블록(```), 설명 텍스트, 인사말을 절대 포함하지 마세요.
3. 응답의 첫 글자는 반드시 {{ 이고 마지막 글자는 }} 이어야 합니다.

## 분석 원칙
- 지표가 상충할 때는 signal을 NEUTRAL로 설정하세요.
- confidence_score는 같은 방향을 가리키는 지표 수에 비례하여 계산하세요.
- reasoning에는 반드시 구체적인 수치(RSI, 현재가, ATR 등)를 포함하세요.
- stop_loss / take_profit은 ATR 기반으로 현실적으로 설정하세요.
- LONG: stop_loss < entry_price < take_profit
- SHORT: take_profit < entry_price < stop_loss

## 반환 JSON 스키마 (이 형식에서 절대 벗어나지 마세요)
{schema}

## 출력 예시
{{"signal":"LONG","confidence":"HIGH","confidence_score":78.5,"entry_price":42000.0,"stop_loss":41400.0,"take_profit":43200.0,"reasoning":"Supertrend 강세 전환, EMA 5/8/13 정배열, RSI 48로 중립권에서 반등, 거래량 1.8배 스파이크 확인됨.","key_risks":["거시경제 불확실성","BTC 도미넌스 상승"],"news_impact":"POSITIVE","indicator_summary":{{"supertrend":"bull","rsi":"neutral","squeeze":"fired_long"}}}}"""


def _build_system_prompt() -> str:
    """JSON 스키마를 시스템 프롬프트에 삽입하여 반환"""
    schema_str = json.dumps(TRADE_SIGNAL_JSON_SCHEMA, ensure_ascii=False, indent=2)
    return _SYSTEM_PROMPT_TEMPLATE.format(schema=schema_str)


# ── JSON 추출 유틸리티 ────────────────────────────────────────────────

def extract_json(text: str) -> str:
    """
    LLM 응답 텍스트에서 순수 JSON 문자열만 추출합니다.

    처리 순서:
      1. 마크다운 코드블록 (```json...``` 또는 ```...```)
      2. 가장 바깥쪽 { } 범위 (첫 번째 '{' ~ 마지막 '}')
      3. 실패 시 원본 텍스트 반환 (호출자가 JSONDecodeError 처리)

    Examples:
      >>> extract_json('```json\\n{"a": 1}\\n```')
      '{"a": 1}'
      >>> extract_json('Sure! Here is the result: {"a": 1} Hope this helps!')
      '{"a": 1}'
      >>> extract_json('{"a": 1}')
      '{"a": 1}'
    """
    text = text.strip()

    # 1단계: 마크다운 코드블록 제거
    code_block = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```", text)
    if code_block:
        return code_block.group(1).strip()

    # 2단계: 첫 { ~ 마지막 } 추출 (중간 텍스트/설명 제거)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]

    # 3단계: 그대로 반환 (JSONDecodeError는 호출자가 처리)
    return text


# ── 메인 분석 클래스 ──────────────────────────────────────────────────

class AIAnalyzer:
    """
    BakktaResult + NewsContext → AIDecision (TradeSignal 포함)

    Groq API를 통해 Llama 3 모델로 분석하며,
    JSON 파싱 실패 시 최대 ai_max_retries회 재시도 후
    실패 시 NEUTRAL 폴백을 반환합니다.

    Usage:
        analyzer = AIAnalyzer()
        decision = await analyzer.analyze("BTCUSDT", bakkta_result, news_items)
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = AsyncOpenAI(
            api_key=self._settings.groq_api_key,
            base_url=self._settings.groq_base_url,
        )
        self._system_prompt = _build_system_prompt()

    async def analyze(
        self,
        symbol: str,
        indicator: BakktaResult,
        news: NewsContext,
    ) -> AIDecision:
        """
        분석을 수행하고 항상 AIDecision을 반환합니다.
        AI 실패 시에도 NEUTRAL 폴백으로 안전하게 반환하며 예외를 전파하지 않습니다.
        """
        start_ms = int(time.time() * 1000)
        initial_prompt = self._build_user_prompt(indicator, news)

        trade_signal, retry_count, is_fallback = await self._analyze_with_retry(
            symbol=symbol,
            initial_prompt=initial_prompt,
            indicator=indicator,
        )

        elapsed = int(time.time() * 1000) - start_ms

        return AIDecision(
            symbol=symbol,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            trade_signal=trade_signal,
            model_version=self._settings.ai_model,
            analysis_duration_ms=elapsed,
            retry_count=retry_count,
            is_fallback=is_fallback,
        )

    # ── 핵심 Retry 루프 ───────────────────────────────────────────────

    async def _analyze_with_retry(
        self,
        symbol: str,
        initial_prompt: str,
        indicator: BakktaResult,
    ) -> tuple[TradeSignal, int, bool]:
        """
        Retry 루프 실행부.

        Returns:
            (TradeSignal, 실제_재시도_횟수, 폴백_여부)

        Retry 전략:
          - 대화 히스토리(messages)에 실패 응답과 에러 메시지를 누적
          - 모델이 자신의 실수를 인지하고 수정하도록 유도
          - 최대 ai_max_retries번 재시도 (기본 2회)
        """
        max_retries: int = self._settings.ai_max_retries
        messages: list[dict[str, str]] = [
            {"role": "user", "content": initial_prompt}
        ]
        last_error = "알 수 없는 오류"

        for attempt in range(max_retries + 1):  # 0(첫시도), 1(1차재시도), 2(2차재시도)
            is_retry = attempt > 0

            if is_retry:
                logger.info(
                    "ai.retry",
                    symbol=symbol,
                    attempt=attempt,
                    max=max_retries,
                    reason=last_error,
                )

            # ── Groq API 호출 ─────────────────────────────────────
            try:
                raw_text = await self._call_groq(messages)
            except Exception as exc:
                last_error = f"API 호출 실패: {exc}"
                logger.error("ai.api_error", symbol=symbol, attempt=attempt, error=last_error)
                if attempt < max_retries:
                    messages = self._append_retry_messages(messages, "", last_error)
                continue

            # ── 안전장치 1: JSON 추출 (Regex) ─────────────────────
            json_str = extract_json(raw_text)

            # ── 안전장치 2: JSON 파싱 + Pydantic 검증 ────────────
            parse_error: str | None = None
            trade_signal: TradeSignal | None = None

            try:
                data: dict[str, Any] = json.loads(json_str)
            except json.JSONDecodeError as exc:
                parse_error = f"JSONDecodeError: {exc.msg} (pos={exc.pos})"
                logger.warning(
                    "ai.json_decode_error",
                    symbol=symbol,
                    attempt=attempt,
                    error=parse_error,
                    raw_preview=raw_text[:200],
                )
            else:
                try:
                    # jsonschema 1차 구조 검증
                    validate(instance=data, schema=TRADE_SIGNAL_JSON_SCHEMA)
                    # Pydantic 2차 도메인 검증 (SL/TP 방향성 등)
                    trade_signal = TradeSignal(**data)
                except JsonSchemaValidationError as exc:
                    parse_error = f"Schema 검증 실패: {exc.message}"
                    logger.warning(
                        "ai.schema_validation_error",
                        symbol=symbol,
                        attempt=attempt,
                        error=parse_error,
                    )
                except PydanticValidationError as exc:
                    # 어떤 필드에서 어떤 에러인지 명확하게 전달
                    field_errors = "; ".join(
                        f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                        for e in exc.errors()
                    )
                    parse_error = f"Pydantic 검증 실패: {field_errors}"
                    logger.warning(
                        "ai.pydantic_error",
                        symbol=symbol,
                        attempt=attempt,
                        error=parse_error,
                    )

            # ── 성공 ──────────────────────────────────────────────
            if trade_signal is not None:
                if is_retry:
                    logger.info(
                        "ai.retry_success",
                        symbol=symbol,
                        attempt=attempt,
                        signal=trade_signal.signal,
                    )
                else:
                    logger.info(
                        "ai.success",
                        symbol=symbol,
                        signal=trade_signal.signal,
                        confidence=trade_signal.confidence,
                        score=trade_signal.confidence_score,
                    )
                return trade_signal, attempt, False

            # ── 재시도 준비 ───────────────────────────────────────
            last_error = parse_error or "파싱 실패"
            if attempt < max_retries:
                messages = self._append_retry_messages(messages, raw_text, last_error)

        # ── 안전장치 3: 최종 폴백 (NEUTRAL) ──────────────────────
        logger.error(
            "ai.all_retries_exhausted",
            symbol=symbol,
            retries=max_retries,
            last_error=last_error,
        )
        fallback = neutral_fallback(
            symbol=symbol,
            entry_price=indicator.close,
            atr=indicator.atr,
            reason=last_error,
        )
        return fallback, max_retries, True

    # ── Groq API 호출 ─────────────────────────────────────────────────

    async def _call_groq(self, messages: list[dict[str, str]]) -> str:
        """
        Groq API (OpenAI 호환 엔드포인트)를 통해 텍스트를 반환합니다.
        temperature=0.1로 설정하여 JSON 출력 형식 일관성 극대화.
        """
        response = await self._client.chat.completions.create(
            model=self._settings.ai_model,
            max_tokens=self._settings.ai_max_tokens,
            temperature=self._settings.ai_temperature,
            messages=[
                {"role": "system", "content": self._system_prompt},
                *messages,
            ],
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("Groq API가 빈 응답을 반환했습니다")
        return content

    # ── 프롬프트 빌더 ─────────────────────────────────────────────────

    @staticmethod
    def _build_user_prompt(indicator: BakktaResult, news: NewsContext) -> str:
        ind = indicator.to_dict()
        news_block = news.to_ai_context()

        return (
            f"다음 데이터를 분석하여 {ind['symbol']} 매매 신호 JSON을 반환하세요.\n\n"
            f"## 기술적 지표 (Bakkta Strategy)\n"
            f"- 심볼: {ind['symbol']}\n"
            f"- 종합방향: {ind['direction']} (score={ind['score']:.1f}/100)\n"
            f"- Supertrend 강세: {ind['supertrend_bull']}\n"
            f"- EMA 리본 정배열: {ind['ema_aligned_bull']}\n"
            f"- RSI: {ind['rsi']:.2f} (35↓과매도 / 65↑과매수)\n"
            f"- RSI 신호: {ind['rsi_signal']}\n"
            f"- 거래량 급등: {ind['volume_spike']}\n"
            f"- Squeeze 발동: {ind['squeeze_fired']} (방향={ind['squeeze_direction']})\n"
            f"- ATR: {ind['atr']:.6f}\n"
            f"- 현재가: {ind['close']}\n"
            f"- 제안 손절거리: {ind['stop_loss_pct']:.4f} ({ind['stop_loss_pct']:.2%})\n"
            f"- 제안 익절거리: {ind['take_profit_pct']:.4f} ({ind['take_profit_pct']:.2%})\n\n"
            f"## 최근 뉴스\n{news_block}\n\n"
            f"위 데이터를 종합하여 순수 JSON만 반환하세요."
        )

    @staticmethod
    def _append_retry_messages(
        messages: list[dict[str, str]],
        failed_response: str,
        error_detail: str,
    ) -> list[dict[str, str]]:
        """
        실패한 응답과 에러 피드백을 대화 히스토리에 추가합니다.
        모델이 자신의 이전 실수를 인지하도록 유도하는 핵심 로직.
        """
        updated = list(messages)

        # 모델의 실패 응답을 assistant 턴으로 추가
        if failed_response:
            updated.append({"role": "assistant", "content": failed_response})

        # 에러 원인과 교정 요청을 user 턴으로 추가
        feedback = (
            f"⚠️ 네가 방금 보낸 응답에서 JSON 파싱 에러가 발생했습니다.\n"
            f"에러 내용: {error_detail}\n\n"
            f"다음 규칙을 다시 확인하세요:\n"
            f"1. 응답의 첫 글자는 반드시 {{ 이어야 합니다.\n"
            f"2. 마크다운 코드블록(```), 설명 텍스트, 개행 전 텍스트를 포함하지 마세요.\n"
            f"3. LONG이면 stop_loss < entry_price < take_profit 이어야 합니다.\n"
            f"4. SHORT이면 take_profit < entry_price < stop_loss 이어야 합니다.\n"
            f"5. reasoning은 50자 이상이어야 합니다.\n\n"
            f"스키마에 정확히 맞는 순수한 JSON 객체만 다시 반환해 주세요."
        )
        updated.append({"role": "user", "content": feedback})
        return updated
