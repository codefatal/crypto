"""
Reasoning Logger
─────────────────
AI의 매매 판단 근거(Reasoning)를 DB에 영구 저장합니다.

지원 DB:
  SQLite  — 로컬 개발 (파일 자동 생성, 서버 불필요)
            DATABASE_URL=sqlite:///autocrypto.db
  PostgreSQL — 운영 환경 (AWS EC2)
            DATABASE_URL=postgresql+psycopg2://...

SQLite ↔ PostgreSQL 전환은 .env의 DATABASE_URL 변경만으로 완료됩니다.
코드 변경 불필요.

테이블:
  ai_decisions        : AI 분석 전체 기록
  indicator_snapshots : 지표 스냅샷
  trade_signals       : 실행된 주문 기록
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from config import get_settings
from src.ai.schemas import AIDecision
from src.indicator.bakkta import BakktaResult

logger = structlog.get_logger(__name__)


class Base(DeclarativeBase):
    pass


class AIDecisionRecord(Base):
    __tablename__ = "ai_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    signal = Column(String(10), nullable=False)
    confidence = Column(String(10), nullable=False)
    confidence_score = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    reasoning = Column(Text, nullable=False)
    key_risks = Column(JSON, default=list)
    news_impact = Column(String(10), nullable=False)
    indicator_summary = Column(JSON, default=dict)
    model_version = Column(String(50), nullable=False)
    analysis_duration_ms = Column(Integer, nullable=False)
    is_fallback = Column(Boolean, default=False)
    executed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.utcnow())


class IndicatorSnapshotRecord(Base):
    __tablename__ = "indicator_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    exchange = Column(String(20), nullable=False, default="unknown")
    direction = Column(String(10), nullable=False)
    score = Column(Float, nullable=False)
    supertrend_bull = Column(Boolean, nullable=False)
    ema_aligned_bull = Column(Boolean, nullable=False)
    rsi = Column(Float, nullable=False)
    rsi_signal = Column(String(10), nullable=False)
    volume_spike = Column(Boolean, nullable=False)
    squeeze_fired = Column(Boolean, nullable=False)
    squeeze_direction = Column(String(10), nullable=False)
    atr = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.utcnow())


class TradeRecord(Base):
    __tablename__ = "trade_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(Integer, nullable=True)
    symbol = Column(String(20), nullable=False, index=True)
    exchange = Column(String(20), nullable=False, default="unknown")
    side = Column(String(10), nullable=False)
    quantity = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    order_id = Column(String(50), nullable=True)
    status = Column(String(20), default="PENDING")
    filled_price = Column(Float, nullable=True)
    pnl_value = Column(Float, nullable=True)     # KRW(업비트) 또는 USDT(바이낸스)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.utcnow())


# ── Engine 팩토리 ────────────────────────────────────────────────────

def _create_engine_for_url(database_url: str):
    """
    DATABASE_URL에 따라 적절한 SQLAlchemy 엔진을 생성합니다.

    SQLite:
      - check_same_thread=False : asyncio.to_thread에서 멀티스레드 접근 허용
      - StaticPool              : SQLite 파일 기반 단일 커넥션 안정화
      - pool_size / max_overflow 파라미터 사용 불가 → 생략

    PostgreSQL:
      - pool_size=5, max_overflow=10, pool_pre_ping=True
    """
    if database_url.startswith("sqlite"):
        # NullPool: 스레드별 독립 연결 생성 (StaticPool의 단일 연결 경쟁 문제 해소)
        # asyncio.to_thread로 여러 심볼이 동시에 log_indicator를 호출할 때
        # StaticPool의 단일 커넥션을 공유하면 FlushError/rollback 오류 발생
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
            echo=False,
        )
    else:
        return create_engine(
            database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=False,
        )


# ── ReasoningLogger ───────────────────────────────────────────────────

class ReasoningLogger:
    """
    동기 SQLAlchemy 세션 기반 로거.
    asyncio 코드에서는 asyncio.to_thread(logger.log_xxx, ...) 로 호출하세요.

    Usage:
        db = ReasoningLogger()
        db.health_check()
        decision_id = db.log_decision(decision)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._exchange = settings.active_exchange
        self._engine = _create_engine_for_url(settings.database_url)
        self._SessionLocal = sessionmaker(
            bind=self._engine, expire_on_commit=False
        )
        self._init_tables(settings.database_url)

    def _init_tables(self, database_url: str) -> None:
        """
        테이블이 없으면 자동 생성합니다.
        SQLite: DB 파일도 없으면 이 시점에 autocrypto.db 파일이 생성됩니다.
        """
        try:
            Base.metadata.create_all(self._engine)
            db_type = "SQLite" if database_url.startswith("sqlite") else "PostgreSQL"
            logger.info("db.tables_ready", type=db_type, url=database_url)
        except Exception as exc:
            logger.error("db.init_failed", error=str(exc))
            raise

    # ── Public ────────────────────────────────────────────────────────

    def log_decision(self, decision: AIDecision) -> int:
        """AI 판단 기록 저장 후 레코드 ID 반환"""
        sig = decision.trade_signal
        record = AIDecisionRecord(
            symbol=decision.symbol,
            timestamp=datetime.utcnow(),
            signal=sig.signal.value if hasattr(sig.signal, "value") else sig.signal,
            confidence=sig.confidence.value if hasattr(sig.confidence, "value") else sig.confidence,
            confidence_score=sig.confidence_score,
            entry_price=sig.entry_price,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            reasoning=sig.reasoning,
            key_risks=sig.key_risks,
            news_impact=sig.news_impact,
            indicator_summary=sig.indicator_summary,
            model_version=decision.model_version,
            analysis_duration_ms=decision.analysis_duration_ms,
            is_fallback=decision.is_fallback,
        )
        with self._SessionLocal() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            logger.info(
                "db.decision_logged",
                id=record.id,
                symbol=decision.symbol,
                signal=record.signal,
                fallback=decision.is_fallback,
            )
            return record.id

    def log_indicator(
        self, symbol: str, result: BakktaResult, exchange: str | None = None
    ) -> None:
        """지표 스냅샷 저장"""
        record = IndicatorSnapshotRecord(
            symbol=symbol,
            timestamp=datetime.utcnow(),
            exchange=exchange or self._exchange,
            direction=result.direction,
            score=result.score,
            supertrend_bull=result.supertrend_bull,
            ema_aligned_bull=result.ema_aligned_bull,
            rsi=result.rsi,
            rsi_signal=result.rsi_signal,
            volume_spike=result.volume_spike,
            squeeze_fired=result.squeeze_fired,
            squeeze_direction=result.squeeze_direction,
            atr=result.atr,
            close=result.close,
        )
        with self._SessionLocal() as session:
            session.add(record)
            session.commit()

    def log_trade(
        self,
        decision_id: int,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        order_id: str | None = None,
        exchange: str | None = None,
    ) -> int:
        """주문 기록 저장 후 레코드 ID 반환"""
        record = TradeRecord(
            decision_id=decision_id,
            symbol=symbol,
            exchange=exchange or self._exchange,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            order_id=order_id,
            status="PLACED" if order_id else "PENDING",
        )
        with self._SessionLocal() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id

    def update_trade_status(
        self,
        trade_id: int,
        status: str,
        filled_price: float | None = None,
        pnl_value: float | None = None,
    ) -> None:
        with self._SessionLocal() as session:
            record = session.get(TradeRecord, trade_id)
            if record:
                record.status = status
                if filled_price is not None:
                    record.filled_price = filled_price
                if pnl_value is not None:
                    record.pnl_value = pnl_value
                if status in ("FILLED", "CLOSED", "CANCELLED"):
                    record.closed_at = datetime.utcnow()
                session.commit()

    def mark_decision_executed(self, decision_id: int) -> None:
        with self._SessionLocal() as session:
            record = session.get(AIDecisionRecord, decision_id)
            if record:
                record.executed = True
                session.commit()

    def health_check(self) -> bool:
        """DB 연결 및 테이블 존재 여부 확인"""
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.error("db.health_check_failed", error=str(exc))
            return False
