"""
AIAnalyzer 핵심 로직 단위 테스트
- extract_json(): 다양한 LLM 출력 패턴 처리
- neutral_fallback(): 폴백 객체 유효성
- _append_retry_messages(): 대화 히스토리 누적
"""
import json

import pytest

from src.ai.analyzer import AIAnalyzer, extract_json
from src.ai.schemas import SignalType, neutral_fallback


# ── extract_json 테스트 ───────────────────────────────────────────────

class TestExtractJson:
    def test_plain_json(self):
        text = '{"signal": "LONG", "confidence": "HIGH"}'
        assert extract_json(text) == text

    def test_markdown_json_block(self):
        text = '```json\n{"signal": "LONG"}\n```'
        result = extract_json(text)
        assert result == '{"signal": "LONG"}'
        json.loads(result)  # 실제로 파싱 가능한지 확인

    def test_markdown_block_no_lang(self):
        text = '```\n{"signal": "SHORT"}\n```'
        result = extract_json(text)
        assert result == '{"signal": "SHORT"}'

    def test_json_with_preamble(self):
        text = 'Sure! Here is the analysis result:\n{"signal": "LONG"}\nHope this helps!'
        result = extract_json(text)
        assert result == '{"signal": "LONG"}'
        json.loads(result)

    def test_json_with_trailing_text(self):
        text = '{"signal": "NEUTRAL"} Please note that this is my analysis.'
        result = extract_json(text)
        assert result == '{"signal": "NEUTRAL"}'

    def test_nested_json(self):
        text = '{"signal": "LONG", "indicator_summary": {"rsi": "neutral", "ema": "bull"}}'
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["indicator_summary"]["rsi"] == "neutral"

    def test_markdown_with_preamble(self):
        text = (
            "Based on the analysis:\n"
            "```json\n"
            '{"signal": "SHORT", "confidence": "MEDIUM"}\n'
            "```\n"
            "This is my recommendation."
        )
        result = extract_json(text)
        parsed = json.loads(result)
        assert parsed["signal"] == "SHORT"

    def test_empty_string_returns_empty(self):
        result = extract_json("")
        assert result == ""

    def test_no_json_returns_original(self):
        text = "I cannot provide a signal at this time."
        result = extract_json(text)
        assert result == text


# ── neutral_fallback 테스트 ───────────────────────────────────────────

class TestNeutralFallback:
    def test_returns_neutral_signal(self):
        sig = neutral_fallback(
            symbol="BTCUSDT",
            entry_price=50000.0,
            atr=500.0,
            reason="API 오류",
        )
        assert sig.signal == SignalType.NEUTRAL

    def test_prices_are_positive(self):
        sig = neutral_fallback(
            symbol="ETHUSDT",
            entry_price=3000.0,
            atr=30.0,
            reason="파싱 실패",
        )
        assert sig.entry_price > 0
        assert sig.stop_loss > 0
        assert sig.take_profit > 0

    def test_reasoning_contains_reason(self):
        reason = "JSONDecodeError at pos=42"
        sig = neutral_fallback(
            symbol="SOLUSDT",
            entry_price=100.0,
            atr=1.5,
            reason=reason,
        )
        assert reason[:50] in sig.reasoning

    def test_reasoning_min_length(self):
        sig = neutral_fallback(
            symbol="BNBUSDT",
            entry_price=400.0,
            atr=5.0,
            reason="오류",
        )
        # Pydantic 모델의 min_length=50 통과 확인
        assert len(sig.reasoning) >= 50

    def test_very_small_atr_uses_minimum(self):
        """ATR이 0에 가까울 때도 유효한 가격 차이 유지"""
        sig = neutral_fallback(
            symbol="PEPEUSDT",
            entry_price=0.00001,
            atr=0.0,   # 극소값
            reason="테스트",
        )
        assert sig.stop_loss > 0
        assert sig.take_profit > sig.stop_loss


# ── _append_retry_messages 테스트 ────────────────────────────────────

class TestRetryMessages:
    def test_appends_assistant_and_user(self):
        initial = [{"role": "user", "content": "분석해줘"}]
        updated = AIAnalyzer._append_retry_messages(
            messages=initial,
            failed_response='{"signal": "INVALID"}',
            error_detail="Pydantic 검증 실패: signal 필드 invalid enum value",
        )
        assert len(updated) == 3
        assert updated[1]["role"] == "assistant"
        assert updated[2]["role"] == "user"

    def test_feedback_contains_error_detail(self):
        initial = [{"role": "user", "content": "분석해줘"}]
        error_msg = "JSONDecodeError: Expecting value at pos=5"
        updated = AIAnalyzer._append_retry_messages(
            messages=initial,
            failed_response="```json oops```",
            error_detail=error_msg,
        )
        assert error_msg in updated[2]["content"]

    def test_empty_failed_response_skips_assistant(self):
        """API 호출 자체 실패 시 빈 응답 — assistant 턴 추가하지 않음"""
        initial = [{"role": "user", "content": "분석해줘"}]
        updated = AIAnalyzer._append_retry_messages(
            messages=initial,
            failed_response="",  # 빈 응답
            error_detail="API 호출 실패: timeout",
        )
        # assistant 턴 없이 user 피드백만 추가
        assert len(updated) == 2
        assert updated[1]["role"] == "user"

    def test_original_messages_not_mutated(self):
        """원본 messages 리스트가 변경되지 않는지 확인"""
        original = [{"role": "user", "content": "분석해줘"}]
        original_copy = list(original)
        AIAnalyzer._append_retry_messages(original, "bad response", "error")
        assert original == original_copy
