"""AI Schema 검증 테스트"""
import pytest
from pydantic import ValidationError

from src.ai.schemas import (
    TRADE_SIGNAL_TOOL,
    ConfidenceLevel,
    SignalType,
    TradeSignal,
)


class TestTradeSignalSchema:
    def _valid_long(self) -> dict:
        return {
            "signal": "LONG",
            "confidence": "HIGH",
            "confidence_score": 85.0,
            "entry_price": 100.0,
            "stop_loss": 97.0,
            "take_profit": 106.0,
            "reasoning": "Supertrend 강세, EMA 리본 정렬, RSI 45로 중립권, 거래량 스파이크 감지",
            "key_risks": ["거시경제 불확실성"],
            "news_impact": "POSITIVE",
            "indicator_summary": {"supertrend": "bull", "rsi": "neutral"},
        }

    def test_valid_long_signal(self):
        sig = TradeSignal(**self._valid_long())
        assert sig.signal == SignalType.LONG
        assert sig.confidence == ConfidenceLevel.HIGH

    def test_invalid_long_sl_above_entry(self):
        data = self._valid_long()
        data["stop_loss"] = 105.0  # entry(100) 위에 SL → invalid for LONG
        with pytest.raises(ValidationError):
            TradeSignal(**data)

    def test_invalid_long_tp_below_entry(self):
        data = self._valid_long()
        data["take_profit"] = 95.0  # entry(100) 아래에 TP → invalid for LONG
        with pytest.raises(ValidationError):
            TradeSignal(**data)

    def test_valid_short_signal(self):
        sig = TradeSignal(
            signal="SHORT",
            confidence="MEDIUM",
            confidence_score=60.0,
            entry_price=100.0,
            stop_loss=103.0,   # SHORT: SL > entry
            take_profit=94.0,  # SHORT: TP < entry
            reasoning="Supertrend 약세, EMA 역배열, RSI 72로 과매수 구간 진입으로 숏 신호 발생",
            key_risks=["갑작스러운 급등 가능성"],
            news_impact="NEGATIVE",
            indicator_summary={"supertrend": "bear"},
        )
        assert sig.signal == SignalType.SHORT

    def test_reasoning_min_length(self):
        data = self._valid_long()
        data["reasoning"] = "짧은 이유"  # 50자 미만
        with pytest.raises(ValidationError):
            TradeSignal(**data)

    def test_confidence_score_out_of_range(self):
        data = self._valid_long()
        data["confidence_score"] = 110.0  # 100 초과
        with pytest.raises(ValidationError):
            TradeSignal(**data)

    def test_invalid_signal_enum(self):
        data = self._valid_long()
        data["signal"] = "MAYBE"  # 유효하지 않은 enum 값
        with pytest.raises(ValidationError):
            TradeSignal(**data)


class TestToolSchema:
    def test_tool_has_required_fields(self):
        schema = TRADE_SIGNAL_TOOL["input_schema"]
        required = set(schema["required"])
        expected = {
            "signal", "confidence", "confidence_score",
            "entry_price", "stop_loss", "take_profit",
            "reasoning", "news_impact", "indicator_summary",
        }
        assert expected.issubset(required)

    def test_tool_disallows_additional_properties(self):
        schema = TRADE_SIGNAL_TOOL["input_schema"]
        assert schema.get("additionalProperties") is False
