"""
Central configuration loaded from environment variables via pydantic-settings.
All modules import from here — never read os.environ directly.

거래소 토글:
  ACTIVE_EXCHANGE=upbit   → UpbitScanner + UpbitTrader
  ACTIVE_EXCHANGE=binance → BinanceScanner + BinanceTrader

DB 기본값:
  DATABASE_URL=sqlite:///autocrypto.db  (로컬 개발용 SQLite)
  PostgreSQL 사용 시: postgresql+psycopg2://user:pw@host:5432/dbname
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 거래소 선택 ─────────────────────────────────────────
    active_exchange: Literal["upbit", "binance"] = Field(
        "upbit", alias="ACTIVE_EXCHANGE"
    )

    # ── Upbit (active_exchange=upbit 시 필요) ───────────────
    upbit_access_key: str = Field("", alias="UPBIT_ACCESS_KEY")
    upbit_secret_key: str = Field("", alias="UPBIT_SECRET_KEY")

    # ── Binance (active_exchange=binance 시 필요) ────────────
    binance_api_key: str = Field("", alias="BINANCE_API_KEY")
    binance_secret_key: str = Field("", alias="BINANCE_SECRET_KEY")
    binance_testnet: bool = Field(False, alias="BINANCE_TESTNET")

    # ── AI (Groq) ────────────────────────────────────────────
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")
    groq_base_url: str = Field(
        "https://api.groq.com/openai/v1", alias="GROQ_BASE_URL"
    )
    ai_model: str = Field("llama-3.3-70b-versatile", alias="AI_MODEL")
    ai_max_tokens: int = Field(2048, alias="AI_MAX_TOKENS")
    ai_temperature: float = Field(0.1, alias="AI_TEMPERATURE")
    ai_max_retries: int = Field(2, alias="AI_MAX_RETRIES")

    # ── Database ─────────────────────────────────────────────
    # 로컬: sqlite:///autocrypto.db
    # 운영: postgresql+psycopg2://user:pw@host:5432/autocrypto
    database_url: str = Field(
        "sqlite:///autocrypto.db", alias="DATABASE_URL"
    )

    # ── Discord ──────────────────────────────────────────────
    discord_webhook_url: str = Field("", alias="DISCORD_WEBHOOK_URL")
    discord_signal_webhook_url: str = Field(
        "", alias="DISCORD_SIGNAL_WEBHOOK_URL"
    )

    # ── Telegram ─────────────────────────────────────────────
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="TELEGRAM_CHAT_ID")

    # ── News ─────────────────────────────────────────────────
    naver_client_id: str = Field("", alias="NAVER_CLIENT_ID")
    naver_client_secret: str = Field("", alias="NAVER_CLIENT_SECRET")

    # ── Trading ──────────────────────────────────────────────
    trade_enabled: bool = Field(False, alias="TRADE_ENABLED")
    max_open_positions: int = Field(5, alias="MAX_OPEN_POSITIONS")
    risk_per_trade_pct: float = Field(0.02, alias="RISK_PER_TRADE_PCT")

    # Upbit 전용 (KRW 기준)
    max_position_krw: float = Field(100_000.0, alias="MAX_POSITION_KRW")
    min_volume_krw: float = Field(1_000_000_000.0, alias="MIN_VOLUME_KRW")

    # Binance 전용 (USDT 기준)
    max_position_usdt: float = Field(100.0, alias="MAX_POSITION_USDT")
    min_volume_usdt: float = Field(1_000_000.0, alias="MIN_VOLUME_USDT")

    # ── Scanner ──────────────────────────────────────────────
    scan_interval_sec: int = Field(60, alias="SCAN_INTERVAL_SEC")
    # Upbit: "minute15" / Binance: "15m" — 각 스캐너가 내부 변환 처리
    timeframe: str = Field("minute15", alias="TIMEFRAME")

    # ── Logging ──────────────────────────────────────────────
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # ── 런타임 검증 ──────────────────────────────────────────
    @field_validator("active_exchange", mode="after")
    @classmethod
    def _validate_exchange(cls, v: str) -> str:
        if v not in ("upbit", "binance"):
            raise ValueError(
                f"ACTIVE_EXCHANGE는 'upbit' 또는 'binance'이어야 합니다. 받은 값: {v!r}"
            )
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
