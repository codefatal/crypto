"""
Tests for src/indicator/technical.py — breakout signal detection
"""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.indicator.technical import (
    _BREAKOUT_MIN_CONDITIONS,
    _MIN_CANDLES,
    check_breakout_signals,
    compute_indicators,
)


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _make_df(n: int = 60, seed: int = 42) -> pd.DataFrame:
    """재현 가능한 OHLCV 더미 DataFrame 생성"""
    rng = np.random.default_rng(seed)
    close = pd.Series(np.cumsum(rng.normal(0, 0.5, n)) + 100.0)
    spread = np.abs(rng.normal(0.3, 0.1, n))
    return pd.DataFrame({
        "open":   close,
        "high":   close + spread,
        "low":    close - spread,
        "close":  close,
        "volume": pd.Series(rng.integers(1_000, 4_000, n).astype(float)),
    })


# ── compute_indicators ────────────────────────────────────────────────────────

class TestComputeIndicators:
    def test_returns_five_keys(self):
        df = _make_df()
        result = compute_indicators(df)
        assert result is not None
        assert set(result.keys()) == {"rsi", "macd", "stoch_rsi_k", "volume_ratio", "adx"}

    def test_returns_none_when_too_few_candles(self):
        df = _make_df(n=_MIN_CANDLES - 1)
        assert compute_indicators(df) is None

    def test_returns_none_on_empty(self):
        assert compute_indicators(pd.DataFrame()) is None

    def test_rsi_in_valid_range(self):
        df = _make_df()
        result = compute_indicators(df)
        rsi = result["rsi"]
        assert not math.isnan(rsi)
        assert 0 <= rsi <= 100

    def test_adx_non_negative(self):
        df = _make_df()
        result = compute_indicators(df)
        adx = result["adx"]
        assert not math.isnan(adx)
        assert adx >= 0

    def test_stoch_rsi_k_in_0_100_range(self):
        df = _make_df()
        result = compute_indicators(df)
        k = result["stoch_rsi_k"]
        if not math.isnan(k):
            assert 0.0 <= k <= 100.0

    def test_volume_ratio_positive(self):
        df = _make_df()
        result = compute_indicators(df)
        ratio = result["volume_ratio"]
        if not math.isnan(ratio):
            assert ratio > 0


# ── check_breakout_signals ────────────────────────────────────────────────────

class TestCheckBreakoutSignals:
    def test_returns_tuple_of_three(self):
        df = _make_df()
        result = check_breakout_signals(df)
        assert len(result) == 3

    def test_false_on_insufficient_data(self):
        df = _make_df(n=10)
        triggered, conditions, values = check_breakout_signals(df)
        assert triggered is False
        assert conditions == []
        assert values == {}

    def test_rsi_condition_detected_but_not_triggered(self):
        """RSI 조건 1개만 충족 — 3개 미만이므로 triggered=False"""
        df = _make_df()
        with patch("src.indicator.technical.compute_indicators") as mock_compute:
            mock_compute.return_value = {
                "rsi": 55.0,          # > 50 ✓
                "macd": -0.5,         # < 0 ✗
                "stoch_rsi_k": 30.0,  # < 50 ✗
                "volume_ratio": 1.2,  # < 2 ✗
                "adx": 10.0,          # < 20 ✗
            }
            triggered, conditions, values = check_breakout_signals(df)

        assert triggered is False      # 1개 < 3개 → 미발동
        assert len(conditions) == 1
        assert conditions[0]["key"] == "rsi"

    def test_three_conditions_trigger(self):
        """3개 이상 조건 충족 시 triggered=True (_BREAKOUT_MIN_CONDITIONS 기준)"""
        df = _make_df()
        with patch("src.indicator.technical.compute_indicators") as mock_compute:
            mock_compute.return_value = {
                "rsi": 55.0,          # ✓
                "macd": 0.5,          # ✓
                "stoch_rsi_k": 60.0,  # ✓
                "volume_ratio": 1.2,  # ✗
                "adx": 10.0,          # ✗
            }
            triggered, conditions, _ = check_breakout_signals(df)

        assert triggered is True
        assert len(conditions) == _BREAKOUT_MIN_CONDITIONS

    def test_volume_surge_alone_not_triggered(self):
        """거래량 조건 1개만 충족 — triggered=False"""
        df = _make_df()
        with patch("src.indicator.technical.compute_indicators") as mock_compute:
            mock_compute.return_value = {
                "rsi": 45.0,
                "macd": -0.1,
                "stoch_rsi_k": 40.0,
                "volume_ratio": 2.5,  # ≥ 2.0 ✓
                "adx": 15.0,
            }
            triggered, conditions, _ = check_breakout_signals(df)

        assert triggered is False
        assert any(c["key"] == "volume_ratio" for c in conditions)

    def test_no_condition_returns_false(self):
        """모든 조건 미충족 → False"""
        df = _make_df()
        with patch("src.indicator.technical.compute_indicators") as mock_compute:
            mock_compute.return_value = {
                "rsi": 40.0,
                "macd": -1.0,
                "stoch_rsi_k": 20.0,
                "volume_ratio": 0.8,
                "adx": 10.0,
            }
            triggered, conditions, _ = check_breakout_signals(df)

        assert triggered is False
        assert conditions == []

    def test_multiple_conditions_all_returned(self):
        """여러 조건 동시 충족 시 모두 반환"""
        df = _make_df()
        with patch("src.indicator.technical.compute_indicators") as mock_compute:
            mock_compute.return_value = {
                "rsi": 60.0,           # ✓
                "macd": 0.5,           # ✓
                "stoch_rsi_k": 70.0,   # ✓
                "volume_ratio": 3.0,   # ✓
                "adx": 25.0,           # ✓
            }
            triggered, conditions, _ = check_breakout_signals(df)

        assert triggered is True
        assert len(conditions) == 5
        keys = {c["key"] for c in conditions}
        assert keys == {"rsi", "macd", "stoch_rsi_k", "volume_ratio", "adx"}

    def test_nan_values_do_not_trigger(self):
        """NaN 값은 조건 미충족으로 처리"""
        df = _make_df()
        with patch("src.indicator.technical.compute_indicators") as mock_compute:
            mock_compute.return_value = {
                "rsi": float("nan"),
                "macd": float("nan"),
                "stoch_rsi_k": float("nan"),
                "volume_ratio": float("nan"),
                "adx": float("nan"),
            }
            triggered, conditions, _ = check_breakout_signals(df)

        assert triggered is False
        assert conditions == []

    def test_condition_name_contains_value(self):
        """조건명에 현재 값이 포함되어 있는지 확인"""
        df = _make_df()
        with patch("src.indicator.technical.compute_indicators") as mock_compute:
            mock_compute.return_value = {
                "rsi": 55.2,
                "macd": -0.1,
                "stoch_rsi_k": 20.0,
                "volume_ratio": 0.8,
                "adx": 10.0,
            }
            _, conditions, _ = check_breakout_signals(df)

        assert len(conditions) == 1
        assert "55.2" in conditions[0]["name"]


# ── Notifier.send_breakout_alert ──────────────────────────────────────────────

class TestNotifierBreakoutAlert:
    @pytest.mark.asyncio
    async def test_calls_discord_and_telegram(self):
        from src.execution.notifier import Notifier
        notifier = Notifier()
        conditions = [
            {"name": "RSI 50 돌파 (현재 55.2)", "key": "rsi", "value": 55.2, "threshold": 50},
            {"name": "거래량 폭등 (2.5배)", "key": "volume_ratio", "value": 2.5, "threshold": 2.0},
        ]
        values = {"rsi": 55.2, "macd": -0.1, "stoch_rsi_k": 30.0, "volume_ratio": 2.5, "adx": 18.0}

        with patch.object(notifier, "_discord_breakout", new_callable=AsyncMock) as d_mock, \
             patch.object(notifier, "_telegram_breakout", new_callable=AsyncMock) as t_mock:
            await notifier.send_breakout_alert("KRW-BTC", conditions, values)

        d_mock.assert_called_once_with("KRW-BTC", conditions, values)
        t_mock.assert_called_once_with("KRW-BTC", conditions, values)

    @pytest.mark.asyncio
    async def test_discord_embed_contains_symbol_and_conditions(self):
        from src.execution.notifier import Notifier
        notifier = Notifier()
        notifier._settings = MagicMock()
        notifier._settings.discord_signal_webhook_url = "https://discord.com/api/webhooks/test"
        notifier._settings.discord_webhook_url = ""

        posted = {}

        async def fake_post(url, json):
            posted.update(json)
            r = MagicMock(); r.raise_for_status = MagicMock(); return r

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = fake_post

        conditions = [
            {"name": "RSI 50 돌파 (현재 55.2)", "key": "rsi", "value": 55.2, "threshold": 50},
        ]
        values = {"rsi": 55.2, "macd": -0.1, "stoch_rsi_k": 30.0, "volume_ratio": 1.0, "adx": 18.0}

        with patch("src.execution.notifier.httpx.AsyncClient", return_value=mock_client):
            await notifier._discord_breakout("KRW-BTC", conditions, values)

        embed = posted["embeds"][0]
        assert "KRW-BTC" in embed["title"]
        assert "돌파 감지" in embed["title"]
        field_values = " ".join(f["value"] for f in embed["fields"])
        assert "RSI 50 돌파" in field_values
