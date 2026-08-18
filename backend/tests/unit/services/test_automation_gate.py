from dataclasses import replace
from decimal import Decimal

from backend.app.services.automation_gate import (
    AutomationEvidence,
    AutomationGate,
    AutomationStage,
)


def complete_evidence() -> AutomationEvidence:
    return AutomationEvidence(
        readonly_days=7,
        unexplained_sequence_gaps=0,
        rules_invalidated_within_cycle=True,
        quote_replay_rate=Decimal(1),
        structured_rejection_rate=Decimal(1),
        oddpool_prices_used_for_execution=False,
        shadow_days=14,
        eligible_shadow_signals=100,
        online_replay_match_rate=Decimal(1),
        stale_book_uses=0,
        fee_categories_complete=True,
        results_segmented_by_market=True,
        per_trade_limit=Decimal(10),
        per_event_limit=Decimal(25),
        portfolio_limit=Decimal(100),
        automatic_executions=30,
        fee_variance_explained=True,
        reconciliation_rate=Decimal(1),
        partial_hedge_reviews_complete=True,
        unhedged_loss_breaches=0,
        anomaly_free_days=7,
        paired_executions_last_30=29,
        emergency_loss=Decimal(2),
        paired_profit=Decimal(20),
        net_pnl=Decimal(5),
        capital_day_return=Decimal("0.001"),
    )


def test_canary_gate_allows_complete_shadow_evidence_at_safe_caps() -> None:
    decision = AutomationGate().evaluate(AutomationStage.CANARY_AUTO, complete_evidence())

    assert decision.allowed is True
    assert decision.reasons == ()


def test_any_missing_metric_fails_closed() -> None:
    evidence = replace(complete_evidence(), online_replay_match_rate=None)

    decision = AutomationGate().evaluate(AutomationStage.CANARY_AUTO, evidence)

    assert decision.allowed is False
    assert "online replay match rate is missing" in decision.reasons


def test_canary_gate_rejects_limits_above_initial_caps() -> None:
    evidence = replace(complete_evidence(), per_trade_limit=Decimal("10.01"))

    decision = AutomationGate().evaluate(AutomationStage.CANARY_AUTO, evidence)

    assert decision.allowed is False
    assert "per-trade limit exceeds canary cap" in decision.reasons


def test_limited_auto_requires_live_execution_quality_and_positive_returns() -> None:
    gate = AutomationGate()

    assert gate.evaluate(AutomationStage.LIMITED_AUTO, complete_evidence()).allowed is True

    failed = gate.evaluate(
        AutomationStage.LIMITED_AUTO,
        replace(
            complete_evidence(),
            paired_executions_last_30=28,
            capital_day_return=Decimal(0),
        ),
    )
    assert failed.allowed is False
    assert "fewer than 29 of the last 30 executions were paired" in failed.reasons
    assert "capital-day return is not positive" in failed.reasons


def test_unhedged_loss_breach_resets_live_gate() -> None:
    decision = AutomationGate().evaluate(
        AutomationStage.LIMITED_AUTO,
        replace(complete_evidence(), unhedged_loss_breaches=1),
    )

    assert decision.allowed is False
    assert "unhedged loss breach count is not zero" in decision.reasons
