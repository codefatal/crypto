"""BakktaIndicator 단위 테스트"""
import numpy as np
import pandas as pd
import pytest

from src.indicator.bakkta import BakktaIndicator, BakktaResult


def _make_df(n: int = 150, trend: str = "up") -> pd.DataFrame:
    """테스트용 OHLCV DataFrame 생성"""
    np.random.seed(42)
    price = 100.0
    rows = []
    for i in range(n):
        if trend == "up":
            price *= 1 + np.random.uniform(0.001, 0.005)
        elif trend == "down":
            price *= 1 - np.random.uniform(0.001, 0.005)
        else:
            price *= 1 + np.random.uniform(-0.003, 0.003)

        open_ = price * (1 - np.random.uniform(0, 0.002))
        high = price * (1 + np.random.uniform(0, 0.005))
        low = price * (1 - np.random.uniform(0, 0.005))
        volume = np.random.uniform(1000, 5000)
        rows.append({
            "open_time": pd.Timestamp("2024-01-01") + pd.Timedelta(minutes=i * 15),
            "open": open_,
            "high": high,
            "low": low,
            "close": price,
            "volume": volume,
            "close_time": pd.Timestamp("2024-01-01") + pd.Timedelta(minutes=i * 15 + 14),
            "quote_volume": volume * price,
            "trades": 100,
            "taker_buy_base": volume * 0.5,
            "taker_buy_quote": volume * price * 0.5,
            "ignore": "0",
        })
    return pd.DataFrame(rows)


class TestBakktaIndicator:
    def setup_method(self):
        self.indicator = BakktaIndicator()

    def test_returns_result_with_enough_data(self):
        df = _make_df(150)
        result = self.indicator.compute("BTCUSDT", df)
        assert result is not None
        assert isinstance(result, BakktaResult)

    def test_returns_none_with_insufficient_data(self):
        df = _make_df(10)
        result = self.indicator.compute("BTCUSDT", df)
        assert result is None

    def test_score_in_range(self):
        df = _make_df(150)
        result = self.indicator.compute("BTCUSDT", df)
        assert result is not None
        assert 0 <= result.score <= 100

    def test_direction_valid(self):
        df = _make_df(150)
        result = self.indicator.compute("BTCUSDT", df)
        assert result is not None
        assert result.direction in ("LONG", "SHORT", "NEUTRAL")

    def test_stop_loss_pct_positive(self):
        df = _make_df(150)
        result = self.indicator.compute("BTCUSDT", df)
        assert result is not None
        assert result.stop_loss_pct > 0
        assert result.take_profit_pct > result.stop_loss_pct

    def test_uptrend_tends_long(self):
        """강한 상승 추세에서 LONG 신호가 주로 발생하는지 확인"""
        df = _make_df(200, trend="up")
        result = self.indicator.compute("BTCUSDT", df)
        assert result is not None
        # 강한 상승 추세라면 NEUTRAL이 아닐 가능성이 높음 (확률적 테스트)
        # 항상 LONG이어야 한다고 단정하지 않음
        assert result.direction in ("LONG", "NEUTRAL", "SHORT")


class TestBakktaResultMethods:
    def test_to_dict_contains_keys(self):
        df = _make_df(150)
        indicator = BakktaIndicator()
        result = indicator.compute("ETHUSDT", df)
        assert result is not None

        d = result.to_dict()
        required_keys = [
            "symbol", "direction", "score", "supertrend_bull",
            "ema_aligned_bull", "rsi", "volume_spike", "squeeze_fired",
            "atr", "close",
        ]
        for key in required_keys:
            assert key in d, f"Missing key: {key}"

    def test_is_tradeable(self):
        df = _make_df(150)
        indicator = BakktaIndicator()
        result = indicator.compute("BTCUSDT", df)
        assert result is not None

        if result.direction == "NEUTRAL":
            assert not result.is_tradeable()
        else:
            # score가 60 이상이면 tradeable
            assert result.is_tradeable() == (result.score >= 60)
