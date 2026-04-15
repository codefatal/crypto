"""
JSON Schema 정의 — AI 할루시네이션 방지용 구조화 출력
──────────────────────────────────────────────────────
Pydantic 모델 : 파이썬 내부 타입 검증
JSON Schema   : 시스템 프롬프트에 직접 삽입 (Groq/Llama 대응)
TRADE_SIGNAL_TOOL : 레거시 호환 유지 (테스트 코드 참조)

neutral_fallback() : AI 파싱 완전 실패 시 안전 NEUTRAL 객체 반환
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SignalType(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TradeSignal(BaseModel):
    """AI가 반환해야 하는 최종 매매 신호"""

    signal: SignalType = Field(
        ...,
        description="매매 방향: LONG(매수), SHORT(매도), NEUTRAL(관망)",
    )
    confidence: ConfidenceLevel = Field(
        ...,
        description="신호 신뢰도: HIGH(70점+), MEDIUM(50~69점), LOW(50점 미만)",
    )
    confidence_score: float = Field(
        ..., ge=0.0, le=100.0,
        description="신뢰도 점수 0~100",
    )
    entry_price: float = Field(
        ..., gt=0,
        description="진입 희망 가격 (현재가 기준)",
    )
    stop_loss: float = Field(
        ..., gt=0,
        description="손절 가격",
    )
    take_profit: float = Field(
        ..., gt=0,
        description="익절 가격",
    )
    reasoning: str = Field(
        ..., min_length=50, max_length=1000,
        description="판단 근거 (기술적 분석 + 뉴스 요약, 50자 이상)",
    )
    key_risks: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="주요 리스크 요인 목록 (최대 5개)",
    )
    news_impact: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"] = Field(
        ...,
        description="뉴스가 신호에 미치는 영향",
    )
    indicator_summary: dict[str, str] = Field(
        default_factory=dict,
        description="각 지표별 판단 요약 {indicator_name: summary}",
    )

    @model_validator(mode="after")
    def validate_sl_tp(self) -> "TradeSignal":
        if self.signal == SignalType.LONG:
            if self.stop_loss >= self.entry_price:
                raise ValueError("LONG 포지션: stop_loss < entry_price 이어야 합니다")
            if self.take_profit <= self.entry_price:
                raise ValueError("LONG 포지션: take_profit > entry_price 이어야 합니다")
        elif self.signal == SignalType.SHORT:
            if self.stop_loss <= self.entry_price:
                raise ValueError("SHORT 포지션: stop_loss > entry_price 이어야 합니다")
            if self.take_profit >= self.entry_price:
                raise ValueError("SHORT 포지션: take_profit < entry_price 이어야 합니다")
        return self


class AIDecision(BaseModel):
    """분석 요청에 대한 전체 AI 응답"""

    symbol: str = Field(..., description="분석 대상 심볼 (예: BTCUSDT)")
    timestamp: str = Field(..., description="분석 시각 (ISO 8601)")
    trade_signal: TradeSignal
    model_version: str = Field(..., description="사용된 AI 모델 이름")
    analysis_duration_ms: int = Field(..., ge=0, description="분석 소요 시간(ms)")
    retry_count: int = Field(0, ge=0, description="파싱 재시도 횟수")
    is_fallback: bool = Field(False, description="True이면 AI 실패로 NEUTRAL 폴백 반환")


def neutral_fallback(
    symbol: str,
    entry_price: float,
    atr: float,
    reason: str,
) -> TradeSignal:
    """
    AI 분석이 완전히 실패했을 때 반환하는 안전 NEUTRAL 신호.
    NEUTRAL 방향이므로 SL/TP validator를 통과하도록 LONG 방향 기준으로 설정.
    (NEUTRAL은 실제로 거래를 실행하지 않으므로 가격 배치는 의미 없음)
    """
    sl_dist = max(atr * 1.5, entry_price * 0.01)  # 최소 1% 거리 보장
    return TradeSignal(
        signal=SignalType.NEUTRAL,
        confidence=ConfidenceLevel.LOW,
        confidence_score=0.0,
        entry_price=entry_price,
        # NEUTRAL 검증 규칙 없음 — 임의의 유효한 양수값으로 설정
        stop_loss=entry_price - sl_dist,
        take_profit=entry_price + sl_dist * 2,
        reasoning=f"[FALLBACK] AI 응답 파싱 최종 실패로 관망 신호 반환. 사유: {reason[:200]}",
        key_risks=["AI 파싱 실패", "수동 확인 필요"],
        news_impact="NEUTRAL",
        indicator_summary={"status": "fallback", "reason": "parse_error"},
    )


# ── 프롬프트 삽입용 JSON Schema (Groq/Llama 대응) ──────────────────────

TRADE_SIGNAL_JSON_SCHEMA: dict = {
    "type": "object",
    "required": [
        "signal", "confidence", "confidence_score",
        "entry_price", "stop_loss", "take_profit",
        "reasoning", "news_impact", "indicator_summary",
    ],
    "additionalProperties": False,
    "properties": {
        "signal": {
            "type": "string",
            "enum": ["LONG", "SHORT", "NEUTRAL"],
            "description": "매매 방향",
        },
        "confidence": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW"],
            "description": "신뢰도 등급",
        },
        "confidence_score": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
            "description": "신뢰도 점수 0~100",
        },
        "entry_price": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "진입 희망 가격",
        },
        "stop_loss": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "손절 가격",
        },
        "take_profit": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "익절 가격",
        },
        "reasoning": {
            "type": "string",
            "minLength": 50,
            "maxLength": 1000,
            "description": "판단 근거 (지표 + 뉴스 기반, 50자 이상)",
        },
        "key_risks": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
            "description": "주요 리스크 요인",
        },
        "news_impact": {
            "type": "string",
            "enum": ["POSITIVE", "NEGATIVE", "NEUTRAL"],
            "description": "뉴스 영향도",
        },
        "indicator_summary": {
            "type": "object",
            "description": "지표별 판단 요약",
            "additionalProperties": {"type": "string"},
        },
    },
}


# ── 레거시 호환 (기존 테스트 코드가 TRADE_SIGNAL_TOOL을 직접 참조) ────

TRADE_SIGNAL_TOOL = {
    "name": "emit_trade_signal",
    "description": (
        "기술적 지표와 뉴스 데이터를 분석하여 암호화폐 매매 신호를 구조화된 JSON으로 반환합니다. "
        "반드시 이 tool을 호출하여 결과를 반환하세요. 자유 텍스트 응답은 허용되지 않습니다."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "signal", "confidence", "confidence_score",
            "entry_price", "stop_loss", "take_profit",
            "reasoning", "news_impact", "indicator_summary",
        ],
        "additionalProperties": False,
        "properties": {
            "signal": {
                "type": "string",
                "enum": ["LONG", "SHORT", "NEUTRAL"],
                "description": "매매 방향",
            },
            "confidence": {
                "type": "string",
                "enum": ["HIGH", "MEDIUM", "LOW"],
                "description": "신뢰도 등급",
            },
            "confidence_score": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "신뢰도 점수 0~100",
            },
            "entry_price": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "진입 희망 가격",
            },
            "stop_loss": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "손절 가격",
            },
            "take_profit": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "익절 가격",
            },
            "reasoning": {
                "type": "string",
                "minLength": 50,
                "maxLength": 1000,
                "description": "판단 근거 (지표 + 뉴스 기반, 50자 이상)",
            },
            "key_risks": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
                "description": "주요 리스크 요인",
            },
            "news_impact": {
                "type": "string",
                "enum": ["POSITIVE", "NEGATIVE", "NEUTRAL"],
                "description": "뉴스 영향도",
            },
            "indicator_summary": {
                "type": "object",
                "description": "지표별 판단 요약",
                "additionalProperties": {"type": "string"},
            },
        },
    },
}
