"""
Rule-based Technical Breakout & Extreme Detector
─────────────────────────────────────────────────
pandas-ta \ub300\uc2e0 `ta` \ub77c\uc774\ube0c\ub7ec\ub9ac \uc0ac\uc6a9 (Python 3.14 \ud638\ud658, numba \ubd88\ud544\uc694).

check_breakout_signals(): 5\uac1c \uc9c0\ud45c AND \uc870\uac74 \ub3cc\ud30c \ud310\ub2e8
detect_market_extremes(): \ud328\ub2c9\uc140 / \uc800\uc810(\uc800\ud3c9\uac00) / \ubc18\ub4f1 \uad6c\uac04 \uac10\uc9c0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog
import ta

logger = structlog.get_logger(__name__)

# ── 조건 임계값 ────────────────────────────────────────────────────────
_THRESHOLDS = {
    "rsi":           50.0,
    "macd":          0.0,
    "stoch_rsi_k":   50.0,   # ta 라이브러리는 0~1 → ×100 후 비교
    "volume_ratio":  2.0,    # 현재 거래량 / 20봉 MA 비율
    "adx":           20.0,
}

# 최소 캔들 수 (ADX/MACD 계산에 필요한 최솟값 보장)
_MIN_CANDLES = 35

# 돌파 알림 발동 최소 조건 수 (5개 중 N개 이상 충족 시 알림)
_BREAKOUT_MIN_CONDITIONS = 5


# ── 공개 함수 ──────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict[str, float] | None:
    """
    OHLCV DataFrame에서 5개 지표의 최신 값을 계산합니다.

    Args:
        df: open_time, open, high, low, close, volume 컬럼을 가진 DataFrame
            (마지막 행 = 최신 확정 캔들)

    Returns:
        {rsi, macd, stoch_rsi_k, volume_ratio, adx} 또는
        None (데이터 부족 / 계산 불가)
    """
    if df is None or len(df) < _MIN_CANDLES:
        return None

    close  = df["close"].astype(float)
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    volume = df["volume"].astype(float)

    try:
        # RSI(14)
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]

        # MACD(12, 26, 9) — MACD 라인 값
        macd_val = ta.trend.MACD(
            close, window_slow=26, window_fast=12, window_sign=9
        ).macd().iloc[-1]

        # StochRSI(14, 14, 3, 3) — K선 0~1 → ×100
        stoch_k_raw = ta.momentum.StochRSIIndicator(
            close, window=14, smooth1=3, smooth2=3
        ).stochrsi_k().iloc[-1]
        stoch_k_val = stoch_k_raw * 100.0 if not _is_nan(stoch_k_raw) else float("nan")

        # Volume MA(20) — 비율로 변환
        vol_ma = volume.rolling(window=20).mean().iloc[-1]
        vol_ratio = (volume.iloc[-1] / vol_ma) if (not _is_nan(vol_ma) and vol_ma > 0) else float("nan")

        # ADX(14)
        adx_val = ta.trend.ADXIndicator(high, low, close, window=14).adx().iloc[-1]

    except Exception as exc:
        logger.warning("technical.compute_failed", error=str(exc))
        return None

    values: dict[str, float] = {
        "rsi":          _round(rsi_val),
        "macd":         _round(macd_val, 6),
        "stoch_rsi_k":  _round(stoch_k_val),
        "volume_ratio": _round(vol_ratio, 2),
        "adx":          _round(adx_val),
    }

    # 핵심 지표가 모두 NaN이면 None 반환
    if all(_is_nan(v) for v in values.values()):
        return None

    return values


def check_breakout_signals(
    df: pd.DataFrame,
) -> tuple[bool, list[dict[str, Any]], dict[str, float]]:
    """
    최신 확정 캔들 기준으로 5개 돌파 조건을 OR 검사합니다.

    Returns:
        (triggered, conditions, current_values)

        triggered         : 1개 이상 조건 충족 여부
        conditions        : 충족된 조건 목록 (각 항목: name, key, value, threshold)
        current_values    : 로깅/알림용 전체 지표 현재값 dict
    """
    values = compute_indicators(df)
    if values is None:
        return False, [], {}

    triggered_conditions: list[dict[str, Any]] = []

    # ── 조건 1: RSI > 50 ──────────────────────────────────────────────
    rsi = values["rsi"]
    if not _is_nan(rsi) and rsi > _THRESHOLDS["rsi"]:
        triggered_conditions.append({
            "name": f"RSI 50 돌파 (현재 {rsi:.1f})",
            "key": "rsi",
            "value": rsi,
            "threshold": _THRESHOLDS["rsi"],
        })

    # ── 조건 2: MACD > 0 ─────────────────────────────────────────────
    macd = values["macd"]
    if not _is_nan(macd) and macd > _THRESHOLDS["macd"]:
        triggered_conditions.append({
            "name": f"MACD 0선 상향 (현재 {macd:.4f})",
            "key": "macd",
            "value": macd,
            "threshold": _THRESHOLDS["macd"],
        })

    # ── 조건 3: StochRSI K > 50 ──────────────────────────────────────
    stoch_k = values["stoch_rsi_k"]
    if not _is_nan(stoch_k) and stoch_k > _THRESHOLDS["stoch_rsi_k"]:
        triggered_conditions.append({
            "name": f"StochRSI K 50 돌파 (현재 {stoch_k:.1f})",
            "key": "stoch_rsi_k",
            "value": stoch_k,
            "threshold": _THRESHOLDS["stoch_rsi_k"],
        })

    # ── 조건 4: 거래량 ≥ Volume MA × 2 ──────────────────────────────
    vol_ratio = values["volume_ratio"]
    if not _is_nan(vol_ratio) and vol_ratio >= _THRESHOLDS["volume_ratio"]:
        triggered_conditions.append({
            "name": f"거래량 폭등 ({vol_ratio:.1f}배)",
            "key": "volume_ratio",
            "value": vol_ratio,
            "threshold": _THRESHOLDS["volume_ratio"],
        })

    # ── 조건 5: ADX > 20 ─────────────────────────────────────────────
    adx = values["adx"]
    if not _is_nan(adx) and adx > _THRESHOLDS["adx"]:
        triggered_conditions.append({
            "name": f"ADX 20 돌파 (현재 {adx:.1f})",
            "key": "adx",
            "value": adx,
            "threshold": _THRESHOLDS["adx"],
        })

    triggered = len(triggered_conditions) >= _BREAKOUT_MIN_CONDITIONS
    return triggered, triggered_conditions, values


# ── ExtremeSignal ─────────────────────────────────────────────────────

@dataclass
class ExtremeSignal:
    """
    \ud328\ub2c9\uc140 / \uc800\uc810 / \ubc18\ub4f1 \uac10\uc9c0 \uacb0\uacfc \ucee8\ud14c\uc774\ub108.

    type  : "panic_sell" | "undervalued" | "rebound"
    emoji : \uc54c\ub9bc\uc6a9 \uc5d0\ubaa8\uc9c0
    name  : \ud55c\uad6d\uc5b4 \uc2e0\ud638\uba85
    reasons  : \uc870\uac74 \ucda9\uc871 \uc774\uc720 \ubaa9\ub85d
    values   : \ub85c\uae45/\uc54c\ub9bc\uc6a9 \uc9c0\ud45c\uac12 dict
    """
    type: str
    emoji: str
    name: str
    reasons: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)


def detect_market_extremes(df: pd.DataFrame) -> list[ExtremeSignal]:
    """
    OHLCV DataFrame\uc5d0\uc11c \ud328\ub2c9\uc140 / \uc800\ud3c9\uac00 / \ubc18\ub4f1 \uc2e0\ud638\ub97c \uac10\uc9c0\ud569\ub2c8\ub2e4.

    \ud328\ub2c9\uc140  : volume \u2265 vol_MA20 \xd7 3  AND  (close-open)/open \u2264 -3%
    \uc800\ud3c9\uac00  : RSI(14) \u2264 25  AND  close < BB \ud558\ub2e8\ubc34(20,2)
    \ubc18\ub4f1    : StochRSI_K \u2264 20  AND  K \uace8\ub4e0\ud06c\ub85c\uc2a4  AND  \uc591\ubd09  AND  MACD hist \uc0c1\uc2b9

    Returns:
        \uac10\uc9c0\ub41c ExtremeSignal \ubaa9\ub85d (\uc5c6\uc73c\uba74 \ube48 \ub9ac\uc2a4\ud2b8)
    """
    if df is None or len(df) < _MIN_CANDLES:
        return []

    close  = df["close"].astype(float)
    open_  = df["open"].astype(float)
    volume = df["volume"].astype(float)

    signals: list[ExtremeSignal] = []

    try:
        # \uacf5\ud1b5 \uc9c0\ud45c \uacc4\uc0b0
        rsi_series = ta.momentum.RSIIndicator(close, window=14).rsi()
        rsi_cur = rsi_series.iloc[-1]

        macd_ind = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
        macd_hist = macd_ind.macd_diff()

        stoch_ind = ta.momentum.StochRSIIndicator(close, window=14, smooth1=3, smooth2=3)
        stoch_k_raw = stoch_ind.stochrsi_k()
        stoch_d_raw = stoch_ind.stochrsi_d()
        stoch_k_cur  = stoch_k_raw.iloc[-1]  * 100.0 if not _is_nan(stoch_k_raw.iloc[-1]) else float("nan")
        stoch_k_prev = stoch_k_raw.iloc[-2]  * 100.0 if not _is_nan(stoch_k_raw.iloc[-2]) else float("nan")
        stoch_d_cur  = stoch_d_raw.iloc[-1]  * 100.0 if not _is_nan(stoch_d_raw.iloc[-1]) else float("nan")
        stoch_d_prev = stoch_d_raw.iloc[-2]  * 100.0 if not _is_nan(stoch_d_raw.iloc[-2]) else float("nan")

        vol_ma20 = volume.rolling(window=20).mean().iloc[-1]
        vol_cur  = volume.iloc[-1]

        bb_ind = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_lower = bb_ind.bollinger_lband().iloc[-1]

        close_cur  = close.iloc[-1]
        open_cur   = open_.iloc[-1]
        hist_cur   = macd_hist.iloc[-1]
        hist_prev  = macd_hist.iloc[-2]

        # ── \ud328\ub2c9\uc140 : volume \u2265 vol_MA20 \xd7 3  AND  \ubc14\ub514 \ud558\ub77d\ub960 \u2264 -3% ─────────────────
        if (
            not _is_nan(vol_ma20) and vol_ma20 > 0
            and not _is_nan(vol_cur)
            and not _is_nan(open_cur) and open_cur > 0
            and not _is_nan(close_cur)
        ):
            vol_ratio_20 = vol_cur / vol_ma20
            candle_chg   = (close_cur - open_cur) / open_cur
            if vol_ratio_20 >= 3.0 and candle_chg <= -0.03:
                signals.append(ExtremeSignal(
                    type="panic_sell",
                    emoji="\U0001f6a8",   # 🚨
                    name="\ud328\ub2c9\uc140 \uac10\uc9c0",
                    reasons=[
                        f"\uac70\ub798\ub7c9 {vol_ratio_20:.1f}\ubc30 \ud3ed\ub4f1 (MA20 \xd7 3 \uc774\uc0c1)",
                        f"\uce94\ub4e4 \ud558\ub77d {candle_chg:.2%}",
                    ],
                    values={
                        "volume_ratio_ma20": _round(vol_ratio_20, 2),
                        "candle_change_pct":  _round(candle_chg * 100, 2),
                        "rsi":               _round(rsi_cur),
                    },
                ))

        # ── \uc800\ud3c9\uac00 : RSI \u2264 25  AND  close < BB \ud558\ub2e8\ubc34 ─────────────────────────────
        if (
            not _is_nan(rsi_cur)
            and not _is_nan(bb_lower)
            and not _is_nan(close_cur)
            and rsi_cur <= 25.0
            and close_cur < bb_lower
        ):
            signals.append(ExtremeSignal(
                type="undervalued",
                emoji="\U0001f4a1",  # 💡
                name="\uc800\ud3c9\uac00 \uad6c\uac04 \uac10\uc9c0",
                reasons=[
                    f"RSI {rsi_cur:.1f} (\u264d 25 \uadf9\ub2e8 \uacfc\ub9e4\ub3c4)",
                    f"\uc885\uac00 {close_cur:,.0f} < BB \ud558\ub2e8\ubc34 {bb_lower:,.0f}",
                ],
                values={
                    "rsi":       _round(rsi_cur),
                    "close":     _round(close_cur, 0),
                    "bb_lower":  _round(bb_lower, 0),
                },
            ))

        # ── \ubc18\ub4f1 : StochRSI_K \u2264 20  AND  K \uace8\ub4e0\ud06c\ub85c\uc2a4  AND  \uc591\ubd09  AND  MACD hist \uc0c1\uc2b9 ─────
        golden_cross = (
            not _is_nan(stoch_k_prev) and not _is_nan(stoch_d_prev)
            and not _is_nan(stoch_k_cur)  and not _is_nan(stoch_d_cur)
            and stoch_k_prev <= stoch_d_prev
            and stoch_k_cur  >  stoch_d_cur
        )
        bullish_candle = not _is_nan(close_cur) and not _is_nan(open_cur) and close_cur > open_cur
        hist_rising    = (
            not _is_nan(hist_cur) and not _is_nan(hist_prev)
            and hist_cur > hist_prev
        )

        if (
            not _is_nan(stoch_k_cur)
            and stoch_k_cur <= 20.0
            and golden_cross
            and bullish_candle
            and hist_rising
        ):
            signals.append(ExtremeSignal(
                type="rebound",
                emoji="\U0001f680",  # 🚀
                name="\ubc18\ub4f1 \uc2e0\ud638 \uac10\uc9c0",
                reasons=[
                    f"StochRSI K {stoch_k_cur:.1f} \u2264 20 + \uace8\ub4e0\ud06c\ub85c\uc2a4",
                    "\uc591\ubd09 \ud655\uc778",
                    f"MACD \ud788\uc2a4\ud1a0\uadf8\ub7a8 \uc0c1\uc2b9 ({hist_prev:.5f} \u2192 {hist_cur:.5f})",
                ],
                values={
                    "stoch_rsi_k":  _round(stoch_k_cur),
                    "stoch_rsi_d":  _round(stoch_d_cur),
                    "macd_hist":    _round(hist_cur, 6),
                    "rsi":          _round(rsi_cur),
                },
            ))

    except Exception as exc:
        logger.warning("technical.extreme_failed", error=str(exc))

    return signals


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────

def _is_nan(v: Any) -> bool:
    try:
        return v != v  # NaN != NaN
    except Exception:
        return True


def _round(v: Any, digits: int = 2) -> float:
    if _is_nan(v):
        return float("nan")
    return round(float(v), digits)
