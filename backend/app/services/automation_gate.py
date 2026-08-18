from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class AutomationStage(StrEnum):
    CANARY_AUTO = "canary_auto"
    LIMITED_AUTO = "limited_auto"


@dataclass(frozen=True, slots=True)
class AutomationEvidence:
    readonly_days: int | None = None
    unexplained_sequence_gaps: int | None = None
    rules_invalidated_within_cycle: bool | None = None
    quote_replay_rate: Decimal | None = None
    structured_rejection_rate: Decimal | None = None
    oddpool_prices_used_for_execution: bool | None = None
    shadow_days: int | None = None
    eligible_shadow_signals: int | None = None
    online_replay_match_rate: Decimal | None = None
    stale_book_uses: int | None = None
    fee_categories_complete: bool | None = None
    results_segmented_by_market: bool | None = None
    per_trade_limit: Decimal | None = None
    per_event_limit: Decimal | None = None
    portfolio_limit: Decimal | None = None
    automatic_executions: int | None = None
    fee_variance_explained: bool | None = None
    reconciliation_rate: Decimal | None = None
    partial_hedge_reviews_complete: bool | None = None
    unhedged_loss_breaches: int | None = None
    anomaly_free_days: int | None = None
    paired_executions_last_30: int | None = None
    emergency_loss: Decimal | None = None
    paired_profit: Decimal | None = None
    net_pnl: Decimal | None = None
    capital_day_return: Decimal | None = None


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reasons: tuple[str, ...]


class AutomationGate:
    def evaluate(
        self,
        stage: AutomationStage,
        evidence: AutomationEvidence,
    ) -> GateDecision:
        reasons = self._readonly_and_shadow_reasons(evidence)
        if stage is AutomationStage.CANARY_AUTO:
            reasons.extend(self._canary_cap_reasons(evidence))
        else:
            reasons.extend(self._live_quality_reasons(evidence))
        return GateDecision(not reasons, tuple(reasons))

    def _readonly_and_shadow_reasons(self, evidence: AutomationEvidence) -> list[str]:
        reasons: list[str] = []
        _minimum(reasons, evidence.readonly_days, 7, "read-only days")
        _equals(reasons, evidence.unexplained_sequence_gaps, 0, "unexplained sequence gap count")
        _true(reasons, evidence.rules_invalidated_within_cycle, "rule invalidation within one cycle")
        _equals(reasons, evidence.quote_replay_rate, Decimal(1), "quote replay rate")
        _equals(
            reasons,
            evidence.structured_rejection_rate,
            Decimal(1),
            "structured rejection rate",
        )
        _false(
            reasons,
            evidence.oddpool_prices_used_for_execution,
            "Oddpool execution-price usage",
        )
        _minimum(reasons, evidence.shadow_days, 14, "shadow days")
        _minimum(reasons, evidence.eligible_shadow_signals, 100, "eligible shadow signal count")
        _equals(
            reasons,
            evidence.online_replay_match_rate,
            Decimal(1),
            "online replay match rate",
        )
        _equals(reasons, evidence.stale_book_uses, 0, "stale book usage count")
        _true(reasons, evidence.fee_categories_complete, "fee category coverage")
        _true(reasons, evidence.results_segmented_by_market, "market category segmentation")
        return reasons

    def _canary_cap_reasons(self, evidence: AutomationEvidence) -> list[str]:
        reasons: list[str] = []
        _maximum(reasons, evidence.per_trade_limit, Decimal(10), "per-trade limit", "canary cap")
        _maximum(reasons, evidence.per_event_limit, Decimal(25), "per-event limit", "canary cap")
        _maximum(reasons, evidence.portfolio_limit, Decimal(100), "portfolio limit", "canary cap")
        return reasons

    def _live_quality_reasons(self, evidence: AutomationEvidence) -> list[str]:
        reasons: list[str] = []
        _minimum(reasons, evidence.automatic_executions, 30, "automatic execution count")
        _true(reasons, evidence.fee_variance_explained, "fee variance explanation")
        _equals(reasons, evidence.reconciliation_rate, Decimal(1), "reconciliation rate")
        _true(
            reasons,
            evidence.partial_hedge_reviews_complete,
            "partial hedge review completion",
        )
        _equals(
            reasons,
            evidence.unhedged_loss_breaches,
            0,
            "unhedged loss breach count",
        )
        _minimum(reasons, evidence.anomaly_free_days, 7, "anomaly-free days")
        if evidence.paired_executions_last_30 is None:
            reasons.append("paired executions in the last 30 is missing")
        elif evidence.paired_executions_last_30 < 29:
            reasons.append("fewer than 29 of the last 30 executions were paired")

        if evidence.emergency_loss is None:
            reasons.append("emergency loss is missing")
        if evidence.paired_profit is None:
            reasons.append("paired profit is missing")
        elif evidence.paired_profit <= 0:
            reasons.append("paired profit is not positive")
        elif (
            evidence.emergency_loss is not None
            and evidence.emergency_loss > evidence.paired_profit * Decimal("0.20")
        ):
            reasons.append("emergency loss exceeds 20% of paired profit")

        _positive(reasons, evidence.net_pnl, "net PnL")
        _positive(reasons, evidence.capital_day_return, "capital-day return")
        return reasons


def _minimum(reasons: list[str], value: int | None, minimum: int, name: str) -> None:
    if value is None:
        reasons.append(f"{name} is missing")
    elif value < minimum:
        reasons.append(f"{name} is below {minimum}")


def _maximum(
    reasons: list[str],
    value: Decimal | None,
    maximum: Decimal,
    name: str,
    threshold_name: str,
) -> None:
    if value is None:
        reasons.append(f"{name} is missing")
    elif value > maximum:
        reasons.append(f"{name} exceeds {threshold_name}")


def _equals(
    reasons: list[str],
    value: int | Decimal | None,
    expected: int | Decimal,
    name: str,
) -> None:
    if value is None:
        reasons.append(f"{name} is missing")
    elif value != expected:
        expected_label = "zero" if expected == 0 else str(expected)
        reasons.append(f"{name} is not {expected_label}")


def _true(reasons: list[str], value: bool | None, name: str) -> None:
    if value is None:
        reasons.append(f"{name} is missing")
    elif not value:
        reasons.append(f"{name} is not complete")


def _false(reasons: list[str], value: bool | None, name: str) -> None:
    if value is None:
        reasons.append(f"{name} is missing")
    elif value:
        reasons.append(f"{name} is not zero")


def _positive(reasons: list[str], value: Decimal | None, name: str) -> None:
    if value is None:
        reasons.append(f"{name} is missing")
    elif value <= 0:
        reasons.append(f"{name} is not positive")
