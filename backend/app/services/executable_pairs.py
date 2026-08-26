from __future__ import annotations

import builtins
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from backend.app.domain.enums import MappingStatus
from backend.app.services.mappings import validate_exact_review


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutablePairInput:
    title: str
    kalshi_market_id: str
    kalshi_outcome: str
    kalshi_rule_text: str
    kalshi_rule_url: str
    polymarket_market_id: str
    polymarket_outcome: str
    polymarket_rule_text: str
    polymarket_rule_url: str
    minimum_quantity: Decimal
    quantity_step: Decimal
    enabled: bool
    kalshi_expected_settlement_at: datetime | None = None
    polymarket_expected_settlement_at: datetime | None = None
    worst_case_settlement_at: datetime | None = None
    kalshi_category: str = ""
    polymarket_category: str = ""
    kalshi_minimum_tick: Decimal = Decimal(0)
    polymarket_minimum_tick: Decimal = Decimal(0)
    native_fingerprint: str = ""
    material_fingerprint: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutablePair(ExecutablePairInput):
    id: str
    status: MappingStatus = MappingStatus.PENDING_REVIEW
    checklist: dict[str, bool] | None = None
    truth_table: tuple[dict[str, Decimal], ...] = ()
    notes: str = ""
    reviewed_by: str | None = None
    source_candidate_id: str | None = None
    source_updated_at: datetime | None = None


class ExecutablePairRepository(Protocol):
    async def list(self) -> list[ExecutablePair]: ...

    async def get(self, pair_id: str) -> ExecutablePair: ...

    async def save(self, pair: ExecutablePair) -> ExecutablePair: ...


class InMemoryExecutablePairRepository:
    def __init__(self) -> None:
        self._pairs: dict[str, ExecutablePair] = {}

    async def list(self) -> list[ExecutablePair]:
        return list(self._pairs.values())

    async def get(self, pair_id: str) -> ExecutablePair:
        return self._pairs[pair_id]

    async def save(self, pair: ExecutablePair) -> ExecutablePair:
        self._pairs[pair.id] = pair
        return pair


class ExecutablePairService:
    def __init__(self, repository: ExecutablePairRepository) -> None:
        self._repository = repository

    async def list(self) -> list[ExecutablePair]:
        return await self._repository.list()

    async def create(self, value: ExecutablePairInput) -> ExecutablePair:
        normalized = _normalize_input(value)
        self._validate_input(normalized)
        return await self._repository.save(ExecutablePair(id=str(uuid4()), **asdict(normalized)))

    async def update(self, pair_id: str, value: ExecutablePairInput) -> ExecutablePair:
        normalized = _normalize_input(value)
        self._validate_input(normalized)
        current = await self._repository.get(pair_id)
        changed = replace(
            current,
            **asdict(normalized),
            status=MappingStatus.PENDING_REVIEW,
            checklist=None,
            truth_table=(),
            notes="",
            reviewed_by=None,
        )
        return await self._repository.save(changed)

    async def upsert_discovered(
        self,
        value: ExecutablePairInput,
        *,
        source_candidate_id: str,
        source_updated_at: datetime,
    ) -> str:
        """Create or refresh one stable review draft from an Oddpool candidate."""
        normalized = _normalize_input(value)
        self._validate_input(normalized)
        pair_id = str(uuid5(NAMESPACE_URL, f"oddpool:{source_candidate_id}"))
        normalized_source_updated_at = _normalized_datetime(source_updated_at)
        try:
            current = await self._repository.get(pair_id)
        except KeyError:
            await self._repository.save(
                ExecutablePair(
                    id=pair_id,
                    **asdict(normalized),
                    source_candidate_id=source_candidate_id,
                    source_updated_at=normalized_source_updated_at,
                )
            )
            return "imported"
        current_material = _material_fingerprint(current)
        current_native = _native_fingerprint(current)
        if (
            current_material == normalized.material_fingerprint
            and current_native == normalized.native_fingerprint
        ):
            return "duplicate"
        if current_material == normalized.material_fingerprint:
            updated = replace(
                current,
                **asdict(normalized),
                source_candidate_id=source_candidate_id,
                source_updated_at=normalized_source_updated_at,
            )
            await self._repository.save(updated)
            return "updated"
        await self._repository.save(
            ExecutablePair(
                id=pair_id,
                **asdict(normalized),
                source_candidate_id=source_candidate_id,
                source_updated_at=normalized_source_updated_at,
            )
        )
        return "updated"

    async def review(
        self,
        pair_id: str,
        *,
        status: MappingStatus,
        checklist: dict[str, bool],
        truth_table: builtins.list[dict[str, Decimal]],
        notes: str,
        reviewer: str,
    ) -> ExecutablePair:
        if status is MappingStatus.EXACT:
            validate_exact_review(checklist, truth_table)
        elif status not in {MappingStatus.CONDITIONAL, MappingStatus.REJECTED}:
            raise ValueError(f"unsupported review status: {status}")
        current = await self._repository.get(pair_id)
        reviewed = replace(
            current,
            status=status,
            checklist=dict(checklist),
            truth_table=tuple(dict(row) for row in truth_table),
            notes=notes,
            reviewed_by=reviewer,
        )
        return await self._repository.save(reviewed)

    async def list_executable(self) -> builtins.list[ExecutablePair]:
        return [
            pair
            for pair in await self._repository.list()
            if pair.enabled and pair.status is MappingStatus.EXACT
        ]

    @staticmethod
    def _validate_input(value: ExecutablePairInput) -> None:
        if value.kalshi_outcome.lower() not in {"yes", "no"}:
            raise ValueError("Kalshi outcome must be yes or no")
        if value.polymarket_outcome.lower() not in {"yes", "no"}:
            raise ValueError("Polymarket outcome must be yes or no")
        if value.minimum_quantity <= 0 or value.quantity_step <= 0:
            raise ValueError("pair quantities must be positive")
        required_text = (
            value.title,
            value.kalshi_market_id,
            value.kalshi_rule_text,
            value.kalshi_rule_url,
            value.polymarket_market_id,
            value.polymarket_rule_text,
            value.polymarket_rule_url,
        )
        if any(not item.strip() for item in required_text):
            raise ValueError("pair text fields must not be blank")


def build_pair_fingerprints(value: ExecutablePairInput) -> tuple[str, str]:
    native_payload = {
        "title": value.title,
        "kalshi_market_id": value.kalshi_market_id,
        "kalshi_outcome": value.kalshi_outcome,
        "kalshi_rule_text": value.kalshi_rule_text,
        "kalshi_rule_url": value.kalshi_rule_url,
        "kalshi_expected_settlement_at": _encoded_datetime(value.kalshi_expected_settlement_at),
        "kalshi_category": value.kalshi_category,
        "kalshi_minimum_tick": _encoded_decimal(value.kalshi_minimum_tick),
        "polymarket_market_id": value.polymarket_market_id,
        "polymarket_outcome": value.polymarket_outcome,
        "polymarket_rule_text": value.polymarket_rule_text,
        "polymarket_rule_url": value.polymarket_rule_url,
        "polymarket_expected_settlement_at": _encoded_datetime(
            value.polymarket_expected_settlement_at
        ),
        "worst_case_settlement_at": _encoded_datetime(value.worst_case_settlement_at),
        "polymarket_category": value.polymarket_category,
        "polymarket_minimum_tick": _encoded_decimal(value.polymarket_minimum_tick),
        "minimum_quantity": _encoded_decimal(value.minimum_quantity),
        "quantity_step": _encoded_decimal(value.quantity_step),
        "enabled": value.enabled,
    }
    material_payload = {
        key: native_payload[key]
        for key in (
            "kalshi_market_id",
            "kalshi_outcome",
            "kalshi_rule_text",
            "kalshi_expected_settlement_at",
            "kalshi_category",
            "kalshi_minimum_tick",
            "polymarket_market_id",
            "polymarket_outcome",
            "polymarket_rule_text",
            "polymarket_expected_settlement_at",
            "worst_case_settlement_at",
            "polymarket_category",
            "polymarket_minimum_tick",
            "minimum_quantity",
            "quantity_step",
        )
    }
    return _fingerprint(native_payload), _fingerprint(material_payload)


def _normalize_input(value: ExecutablePairInput) -> ExecutablePairInput:
    kalshi_expected_settlement_at = _normalized_datetime(value.kalshi_expected_settlement_at)
    polymarket_expected_settlement_at = _normalized_datetime(
        value.polymarket_expected_settlement_at
    )
    worst_case = _normalized_datetime(value.worst_case_settlement_at)
    if worst_case is None:
        settlements = [
            settlement
            for settlement in (
                kalshi_expected_settlement_at,
                polymarket_expected_settlement_at,
            )
            if settlement is not None
        ]
        worst_case = max(settlements, default=None)
    normalized = replace(
        value,
        kalshi_expected_settlement_at=kalshi_expected_settlement_at,
        polymarket_expected_settlement_at=polymarket_expected_settlement_at,
        worst_case_settlement_at=worst_case,
    )
    native_fingerprint, material_fingerprint = build_pair_fingerprints(normalized)
    return replace(
        normalized,
        native_fingerprint=native_fingerprint,
        material_fingerprint=material_fingerprint,
    )


def _native_fingerprint(value: ExecutablePairInput | ExecutablePair) -> str:
    return build_pair_fingerprints(_normalize_input(value))[0]


def _material_fingerprint(value: ExecutablePairInput | ExecutablePair) -> str:
    return build_pair_fingerprints(_normalize_input(value))[1]


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _encoded_datetime(value: datetime | None) -> str | None:
    normalized = _normalized_datetime(value)
    return normalized.isoformat().replace("+00:00", "Z") if normalized is not None else None


def _encoded_decimal(value: Decimal) -> str:
    return format(value, "f")


def _normalized_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
