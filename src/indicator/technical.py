"""
Rule-based Technical Breakout Detector
───────────────────────────────────────
pandas-ta 대신 `ta` 라이브러리 사용 (Python 3.14 호환, numba 불필요).

5가지 지표를 계산하고 OR 조건으로 돌파 여부를 판단합니다:
  1. RSI(14)          > 50
  2. MACD(12,26,9)    > 0   (MACD 라인이 0선 상향)
  3. StochRSI_K(14)   > 50  (K선 0~100 기준)
  4. 거래량           ≥ Volume MA(20) × 2
  5. ADX(14)          > 20  (추세 강도 확인)

Returns:
    (triggered: bool, conditions: list[dict], values: dict)
"""
from __future__ import annotations

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
_BREAKOUT_MIN_CONDITIONS = 3


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
