from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base, IdTimestampMixin

# Financial values retain venue precision and are never stored as IEEE floats.
MONEY = Numeric(38, 18)


class RiskPolicyVersion(Base):
    __tablename__ = "risk_policy_version"

    version: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    minimum_roi: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_settlement_days: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_book_age_seconds: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    per_trade_limit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    per_event_limit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    portfolio_limit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    explicit_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    risk_buffer: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_unhedged_seconds: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_unhedged_loss: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_arrival_gap_seconds: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class VenueMarket(IdTimestampMixin, Base):
    __tablename__ = "venue_market"
    __table_args__ = (UniqueConstraint("venue", "external_id"),)

    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcomes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rule_url: Mapped[str] = mapped_column(Text, nullable=False)
    minimum_tick: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    minimum_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    earliest_close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_settlement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worst_case_settlement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class RuleVersion(IdTimestampMixin, Base):
    __tablename__ = "rule_version"
    __table_args__ = (UniqueConstraint("market_id", "content_hash"),)

    market_id: Mapped[UUID] = mapped_column(ForeignKey("venue_market.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str | None] = mapped_column(Text)
    timezone_name: Mapped[str | None] = mapped_column(String(128))
    cancellation_terms: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PairMapping(IdTimestampMixin, Base):
    __tablename__ = "pair_mapping"
    __table_args__ = (UniqueConstraint("kalshi_market_id", "polymarket_market_id"),)

    kalshi_market_id: Mapped[UUID] = mapped_column(ForeignKey("venue_market.id"), nullable=False)
    polymarket_market_id: Mapped[UUID] = mapped_column(ForeignKey("venue_market.id"), nullable=False)
    kalshi_rule_version_id: Mapped[UUID] = mapped_column(ForeignKey("rule_version.id"), nullable=False)
    polymarket_rule_version_id: Mapped[UUID] = mapped_column(ForeignKey("rule_version.id"), nullable=False)
    kalshi_outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    polymarket_outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MappingReview(IdTimestampMixin, Base):
    __tablename__ = "mapping_review"

    mapping_id: Mapped[UUID] = mapped_column(ForeignKey("pair_mapping.id"), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    checklist: Mapped[dict] = mapped_column(JSONB, nullable=False)
    truth_table: Mapped[dict] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class DiscoveryCandidate(IdTimestampMixin, Base):
    __tablename__ = "discovery_candidate"
    __table_args__ = (UniqueConstraint("source_candidate_id", "source_updated_at"),)

    source_candidate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_link_invalid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class BookSnapshot(IdTimestampMixin, Base):
    __tablename__ = "book_snapshot"

    market_id: Mapped[UUID] = mapped_column(ForeignKey("venue_market.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[str] = mapped_column(String(255), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_safe: Mapped[bool] = mapped_column(Boolean, nullable=False)
    levels: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class QuoteEvaluation(IdTimestampMixin, Base):
    __tablename__ = "quote_evaluation"

    mapping_id: Mapped[UUID] = mapped_column(ForeignKey("pair_mapping.id"), nullable=False)
    kalshi_book_id: Mapped[UUID] = mapped_column(ForeignKey("book_snapshot.id"), nullable=False)
    polymarket_book_id: Mapped[UUID] = mapped_column(ForeignKey("book_snapshot.id"), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    deployed_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    profit_floor: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    conservative_roi: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    rejection_reasons: Mapped[dict] = mapped_column(JSONB, nullable=False)


class BalanceSnapshot(IdTimestampMixin, Base):
    __tablename__ = "balance_snapshot"

    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    available_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    open_order_reserve: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unsettled_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    venue_version: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class Execution(IdTimestampMixin, Base):
    __tablename__ = "execution"

    quote_evaluation_id: Mapped[UUID] = mapped_column(ForeignKey("quote_evaluation.id"), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)


class CapitalReservation(IdTimestampMixin, Base):
    __tablename__ = "capital_reservation"
    __table_args__ = (UniqueConstraint("correlation_id", "venue"),)

    execution_id: Mapped[UUID | None] = mapped_column(ForeignKey("execution.id"))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(255))
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    principal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee_buffer: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    hedge_buffer: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class ExecutionLeg(IdTimestampMixin, Base):
    __tablename__ = "execution_leg"
    __table_args__ = (UniqueConstraint("venue", "client_order_id"),)

    execution_id: Mapped[UUID] = mapped_column(ForeignKey("execution.id"), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class Fill(IdTimestampMixin, Base):
    __tablename__ = "fill"
    __table_args__ = (UniqueConstraint("venue", "venue_fill_id"),)

    execution_leg_id: Mapped[UUID] = mapped_column(ForeignKey("execution_leg.id"), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    venue_fill_id: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class StateTransition(IdTimestampMixin, Base):
    __tablename__ = "state_transition"

    execution_id: Mapped[UUID] = mapped_column(ForeignKey("execution.id"), nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)


class AuditEvent(IdTimestampMixin, Base):
    __tablename__ = "audit_event"

    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    object_type: Mapped[str] = mapped_column(String(128), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    before_state: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class OutboxEvent(IdTimestampMixin, Base):
    __tablename__ = "outbox_event"

    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SystemControl(Base):
    __tablename__ = "system_control"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
