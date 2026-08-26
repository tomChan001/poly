import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.domain.enums import MappingStatus
from backend.app.services.executable_pairs import ExecutablePair


class PostgresExecutablePairRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list(self) -> list[ExecutablePair]:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT snapshot FROM executable_pair ORDER BY updated_at DESC")
            )
            return [_record(row._mapping["snapshot"]) for row in result]

    async def get(self, pair_id: str) -> ExecutablePair:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT snapshot FROM executable_pair WHERE id = CAST(:id AS uuid)"),
                {"id": pair_id},
            )
            row = result.first()
            if row is None:
                raise KeyError(pair_id)
            return _record(row._mapping["snapshot"])

    async def save(self, pair: ExecutablePair) -> ExecutablePair:
        async with self._sessions.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO executable_pair (id, status, enabled, snapshot, updated_at)
                    VALUES (
                        CAST(:id AS uuid), :status, :enabled,
                        CAST(:snapshot AS jsonb), now()
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        enabled = EXCLUDED.enabled,
                        snapshot = EXCLUDED.snapshot,
                        updated_at = now()
                    """
                ),
                {
                    "id": pair.id,
                    "status": pair.status.value,
                    "enabled": pair.enabled,
                    "snapshot": json.dumps(_snapshot(pair)),
                },
            )
        return pair


def _snapshot(pair: ExecutablePair) -> dict[str, object]:
    return {
        "id": pair.id,
        "title": pair.title,
        "kalshi_market_id": pair.kalshi_market_id,
        "kalshi_outcome": pair.kalshi_outcome,
        "kalshi_rule_text": pair.kalshi_rule_text,
        "kalshi_rule_url": pair.kalshi_rule_url,
        "polymarket_market_id": pair.polymarket_market_id,
        "polymarket_outcome": pair.polymarket_outcome,
        "polymarket_rule_text": pair.polymarket_rule_text,
        "polymarket_rule_url": pair.polymarket_rule_url,
        "minimum_quantity": str(pair.minimum_quantity),
        "quantity_step": str(pair.quantity_step),
        "enabled": pair.enabled,
        "kalshi_expected_settlement_at": (
            _normalized_datetime(pair.kalshi_expected_settlement_at).isoformat().replace("+00:00", "Z")
            if pair.kalshi_expected_settlement_at is not None
            else None
        ),
        "polymarket_expected_settlement_at": (
            _normalized_datetime(pair.polymarket_expected_settlement_at).isoformat().replace("+00:00", "Z")
            if pair.polymarket_expected_settlement_at is not None
            else None
        ),
        "worst_case_settlement_at": (
            _normalized_datetime(pair.worst_case_settlement_at).isoformat().replace("+00:00", "Z")
            if pair.worst_case_settlement_at is not None
            else None
        ),
        "kalshi_category": pair.kalshi_category,
        "polymarket_category": pair.polymarket_category,
        "kalshi_minimum_tick": str(pair.kalshi_minimum_tick),
        "polymarket_minimum_tick": str(pair.polymarket_minimum_tick),
        "native_fingerprint": pair.native_fingerprint,
        "material_fingerprint": pair.material_fingerprint,
        "status": pair.status.value,
        "checklist": pair.checklist,
        "truth_table": [
            {name: str(value) for name, value in row.items()}
            for row in pair.truth_table
        ],
        "notes": pair.notes,
        "reviewed_by": pair.reviewed_by,
        "source_candidate_id": pair.source_candidate_id,
        "source_updated_at": (
            _normalized_datetime(pair.source_updated_at).isoformat().replace("+00:00", "Z")
            if pair.source_updated_at is not None
            else None
        ),
    }


def _record(snapshot: dict[str, object]) -> ExecutablePair:
    raw_truth_table = snapshot.get("truth_table", [])
    if not isinstance(raw_truth_table, list):
        raise TypeError("pair truth table must be a list")
    raw_checklist = snapshot.get("checklist")
    checklist = None
    if isinstance(raw_checklist, dict):
        checklist = {str(name): _bool_value(value) for name, value in raw_checklist.items()}
    return ExecutablePair(
        id=str(snapshot["id"]),
        title=str(snapshot["title"]),
        kalshi_market_id=str(snapshot["kalshi_market_id"]),
        kalshi_outcome=str(snapshot["kalshi_outcome"]),
        kalshi_rule_text=str(snapshot["kalshi_rule_text"]),
        kalshi_rule_url=str(snapshot["kalshi_rule_url"]),
        polymarket_market_id=str(snapshot["polymarket_market_id"]),
        polymarket_outcome=str(snapshot["polymarket_outcome"]),
        polymarket_rule_text=str(snapshot["polymarket_rule_text"]),
        polymarket_rule_url=str(snapshot["polymarket_rule_url"]),
        minimum_quantity=Decimal(str(snapshot["minimum_quantity"])),
        quantity_step=Decimal(str(snapshot["quantity_step"])),
        enabled=_bool_value(snapshot["enabled"]),
        kalshi_expected_settlement_at=(
            _parsed_datetime(snapshot["kalshi_expected_settlement_at"])
            if snapshot.get("kalshi_expected_settlement_at") is not None
            else None
        ),
        polymarket_expected_settlement_at=(
            _parsed_datetime(snapshot["polymarket_expected_settlement_at"])
            if snapshot.get("polymarket_expected_settlement_at") is not None
            else None
        ),
        worst_case_settlement_at=(
            _parsed_datetime(snapshot["worst_case_settlement_at"])
            if snapshot.get("worst_case_settlement_at") is not None
            else None
        ),
        kalshi_category=str(snapshot.get("kalshi_category", "")),
        polymarket_category=str(snapshot.get("polymarket_category", "")),
        kalshi_minimum_tick=Decimal(str(snapshot.get("kalshi_minimum_tick", "0"))),
        polymarket_minimum_tick=Decimal(str(snapshot.get("polymarket_minimum_tick", "0"))),
        native_fingerprint=str(snapshot.get("native_fingerprint", "")),
        material_fingerprint=str(snapshot.get("material_fingerprint", "")),
        status=MappingStatus(str(snapshot["status"])),
        checklist=checklist,
        truth_table=tuple(
            {str(name): Decimal(str(value)) for name, value in raw_row.items()}
            for raw_row in raw_truth_table
            if isinstance(raw_row, dict)
        ),
        notes=str(snapshot.get("notes", "")),
        reviewed_by=(
            str(snapshot["reviewed_by"])
            if snapshot.get("reviewed_by") is not None
            else None
        ),
        source_candidate_id=(
            str(snapshot["source_candidate_id"])
            if snapshot.get("source_candidate_id") is not None
            else None
        ),
        source_updated_at=(
            _parsed_datetime(snapshot["source_updated_at"])
            if snapshot.get("source_updated_at") is not None
            else None
        ),
    )


def _parsed_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalized_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise TypeError("datetime value must not be None")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no", ""}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    raise TypeError("snapshot boolean field is invalid")
