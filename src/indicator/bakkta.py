"""
Bakkta Indicator — PineScript → Python
───────────────────────────────────────
Bakkta 전략의 핵심 구성요소:
  1. Supertrend  — 추세 방향 필터
  2. EMA Ribbon  — 5/8/13/21/34/55 EMA 군집
  3. RSI + MA    — 모멘텀 확인
  4. Volume Spike — 거래량 급등 감지
  5. Squeeze Momentum (LazyBear) — 변동성 압축 후 방향 돌파
  6. Signal 집계 — 매수/매도/관망 점수 계산

Returns:
    BakktaResult dataclass (신호 강도 0~100, 방향, 세부 구성요소)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

Direction = Literal["LONG", "SHORT", "NEUTRAL"]


@dataclass
class BakktaResult:
    symbol: str
    direction: Direction
    score: float          # 0~100, 높을수록 강한 신호
    supertrend_bull: bool
    ema_aligned_bull: bool
    rsi: float
    rsi_signal: Direction
    volume_spike: bool
    squeeze_fired: bool
    squeeze_direction: Direction
    atr: float
    stop_loss_pct: float   # ATR 기반 SL 비율
    take_profit_pct: float # 2:1 TP 비율
    close: float

    def is_tradeable(self, min_score: float = 60.0) -> bool:
        return self.direction != "NEUTRAL" and self.score >= min_score

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "score": round(self.score, 2),
            "supertrend_bull": self.supertrend_bull,
            "ema_aligned_bull": self.ema_aligned_bull,
            "rsi": round(self.rsi, 2),
            "rsi_signal": self.rsi_signal,
            "volume_spike": self.volume_spike,
            "squeeze_fired": self.squeeze_fired,
            "squeeze_direction": self.squeeze_direction,
            "atr": round(self.atr, 6),
            "stop_loss_pct": round(self.stop_loss_pct, 4),
            "take_profit_pct": round(self.take_profit_pct, 4),
            "close": self.close,
        }


class BakktaIndicator:
    """
    DataFrame → BakktaResult 변환기.

    Args:
        st_period: Supertrend ATR 기간 (기본 10)
        st_multiplier: Supertrend ATR 배수 (기본 3.0)
        ema_lengths: EMA 리본 기간 리스트
        rsi_period: RSI 기간
        squeeze_length: Squeeze Momentum 기간
        volume_ma_period: 거래량 MA 기간
        volume_spike_multiplier: 거래량 스파이크 배수
    """

    def __init__(
        self,
        st_period: int = 10,
        st_multiplier: float = 3.0,
        ema_lengths: list[int] | None = None,
        rsi_period: int = 14,
        squeeze_length: int = 20,
        volume_ma_period: int = 20,
        volume_spike_multiplier: float = 1.5,
    ) -> None:
        self.st_period = st_period
        self.st_multiplier = st_multiplier
        self.ema_lengths = ema_lengths or [5, 8, 13, 21, 34, 55]
        self.rsi_period = rsi_period
        self.squeeze_length = squeeze_length
        self.volume_ma_period = volume_ma_period
        self.volume_spike_multiplier = volume_spike_multiplier

    def compute(self, symbol: str, df: pd.DataFrame) -> BakktaResult | None:
        """
        df: OHLCV DataFrame (최소 100개 캔들 권장)
        Returns: BakktaResult or None (데이터 부족 시)
        """
        if len(df) < max(self.ema_lengths) + 10:
            return None

        df = df.copy()
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        # 1. Supertrend
        st_bull, atr = self._supertrend(high, low, close)

        # 2. EMA Ribbon
        emas = {length: close.ewm(span=length, adjust=False).mean()
                for length in self.ema_lengths}
        ema_aligned_bull = self._ema_ribbon_aligned(emas, close.iloc[-1])

        # 3. RSI
        rsi_val = self._rsi(close).iloc[-1]
        rsi_signal = _rsi_to_signal(rsi_val)

        # 4. Volume Spike
        vol_ma = volume.rolling(self.volume_ma_period).mean()
        vol_spike = bool(volume.iloc[-1] > vol_ma.iloc[-1] * self.volume_spike_multiplier)

        # 5. Squeeze Momentum
        sq_fired, sq_dir = self._squeeze_momentum(high, low, close)

        # 6. Score 집계
        score, direction = self._aggregate(
            st_bull=st_bull,
            ema_aligned_bull=ema_aligned_bull,
            rsi_signal=rsi_signal,
            vol_spike=vol_spike,
            sq_fired=sq_fired,
            sq_dir=sq_dir,
        )

        atr_val = float(atr.iloc[-1])
        close_val = float(close.iloc[-1])
        sl_pct = (atr_val * 1.5) / close_val
        tp_pct = sl_pct * 2.0

        return BakktaResult(
            symbol=symbol,
            direction=direction,
            score=score,
            supertrend_bull=st_bull,
            ema_aligned_bull=ema_aligned_bull,
            rsi=rsi_val,
            rsi_signal=rsi_signal,
            volume_spike=vol_spike,
            squeeze_fired=sq_fired,
            squeeze_direction=sq_dir,
            atr=atr_val,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            close=close_val,
        )

    # ── Supertrend ────────────────────────────────────────────────────

    def _supertrend(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> tuple[bool, pd.Series]:
        """
        PineScript supertrend() 포팅.
        Returns (is_bullish_on_last_bar, atr_series)
        """
        atr = self._atr(high, low, close, self.st_period)
        hl2 = (high + low) / 2

        upper_band = hl2 + self.st_multiplier * atr
        lower_band = hl2 - self.st_multiplier * atr

        supertrend = pd.Series(np.nan, index=close.index)
        direction_arr = pd.Series(1, index=close.index)  # 1=bull, -1=bear

        for i in range(1, len(close)):
            prev_upper = upper_band.iloc[i - 1]
            prev_lower = lower_band.iloc[i - 1]
            prev_st = supertrend.iloc[i - 1]
            prev_close = close.iloc[i - 1]

            upper_band.iloc[i] = (
                min(upper_band.iloc[i], prev_upper)
                if prev_close <= prev_upper else upper_band.iloc[i]
            )
            lower_band.iloc[i] = (
                max(lower_band.iloc[i], prev_lower)
                if prev_close >= prev_lower else lower_band.iloc[i]
            )

            if pd.isna(prev_st) or prev_st == prev_upper:
                if close.iloc[i] > upper_band.iloc[i]:
                    supertrend.iloc[i] = lower_band.iloc[i]
                    direction_arr.iloc[i] = 1
                else:
                    supertrend.iloc[i] = upper_band.iloc[i]
                    direction_arr.iloc[i] = -1
            else:
                if close.iloc[i] < lower_band.iloc[i]:
                    supertrend.iloc[i] = upper_band.iloc[i]
                    direction_arr.iloc[i] = -1
                else:
                    supertrend.iloc[i] = lower_band.iloc[i]
                    direction_arr.iloc[i] = 1

        is_bull = bool(direction_arr.iloc[-1] == 1)
        return is_bull, atr

    # ── EMA Ribbon ────────────────────────────────────────────────────

    def _ema_ribbon_aligned(
        self, emas: dict[int, pd.Series], latest_close: float
    ) -> bool:
        """
        모든 EMA가 오름차순(짧은 EMA > 긴 EMA) 정렬이면 강세,
        내림차순이면 약세. 마지막 봉 기준.
        """
        vals = [emas[l].iloc[-1] for l in sorted(self.ema_lengths)]
        bull_aligned = all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))
        return bull_aligned

    # ── RSI ───────────────────────────────────────────────────────────

    def _rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(com=self.rsi_period - 1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=self.rsi_period - 1, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    # ── ATR ───────────────────────────────────────────────────────────

    @staticmethod
    def _atr(
        high: pd.Series, low: pd.Series, close: pd.Series, period: int
    ) -> pd.Series:
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, adjust=False).mean()

    # ── Squeeze Momentum (LazyBear) ───────────────────────────────────

    def _squeeze_momentum(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> tuple[bool, Direction]:
        """
        Bollinger Band 안에 Keltner Channel이 포함될 때 = squeeze.
        squeeze 해제 시 momentum 방향으로 fired.
        """
        n = self.squeeze_length
        bb_mult = 2.0
        kc_mult = 1.5

        ma = close.rolling(n).mean()
        std = close.rolling(n).std()
        bb_upper = ma + bb_mult * std
        bb_lower = ma - bb_mult * std

        atr = self._atr(high, low, close, n)
        kc_upper = ma + kc_mult * atr
        kc_lower = ma - kc_mult * atr

        # squeeze: BB가 KC 안에 있는 상태
        sq_on = (bb_upper < kc_upper) & (bb_lower > kc_lower)

        # momentum 값 계산
        highest_high = high.rolling(n).max()
        lowest_low = low.rolling(n).min()
        delta = close - (highest_high + lowest_low) / 2 + ma
        momentum = delta.rolling(n).mean()

        # 마지막 2봉 비교로 fired 여부 판단
        if len(momentum) < 2:
            return False, "NEUTRAL"

        was_squeeze = bool(sq_on.iloc[-2]) if len(sq_on) > 1 else False
        is_squeeze = bool(sq_on.iloc[-1])
        fired = was_squeeze and not is_squeeze

        mom_val = float(momentum.iloc[-1])
        sq_dir: Direction = "LONG" if mom_val > 0 else "SHORT"

        return fired, sq_dir

    # ── Score Aggregation ─────────────────────────────────────────────

    @staticmethod
    def _aggregate(
        st_bull: bool,
        ema_aligned_bull: bool,
        rsi_signal: Direction,
        vol_spike: bool,
        sq_fired: bool,
        sq_dir: Direction,
    ) -> tuple[float, Direction]:
        """
        각 구성요소 점수를 가중합산하여 최종 방향과 점수(0~100) 반환.

        가중치:
          - Supertrend:      30점
          - EMA Ribbon:      25점
          - Squeeze Fired:   20점
          - RSI signal:      15점
          - Volume Spike:    10점
        """
        long_score = 0.0
        short_score = 0.0

        # Supertrend (30점)
        if st_bull:
            long_score += 30
        else:
            short_score += 30

        # EMA Ribbon (25점)
        if ema_aligned_bull:
            long_score += 25
        else:
            short_score += 25

        # Squeeze Momentum (20점 — fired일 때만)
        if sq_fired:
            if sq_dir == "LONG":
                long_score += 20
            elif sq_dir == "SHORT":
                short_score += 20

        # RSI (15점)
        if rsi_signal == "LONG":
            long_score += 15
        elif rsi_signal == "SHORT":
            short_score += 15

        # Volume Spike (10점 — 방향 강화)
        if vol_spike:
            if long_score >= short_score:
                long_score += 10
            else:
                short_score += 10

        if long_score > short_score and long_score >= 50:
            return long_score, "LONG"
        elif short_score > long_score and short_score >= 50:
            return short_score, "SHORT"
        else:
            return max(long_score, short_score), "NEUTRAL"


# ── Helpers ───────────────────────────────────────────────────────────

def _rsi_to_signal(rsi: float) -> Direction:
    if rsi < 35:
        return "LONG"    # 과매도 → 매수 기회
    elif rsi > 65:
        return "SHORT"   # 과매수 → 매도 기회
    return "NEUTRAL"
