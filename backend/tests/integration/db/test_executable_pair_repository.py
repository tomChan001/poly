import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.executable_pairs import PostgresExecutablePairRepository, _record
from backend.app.domain.enums import MappingStatus
from backend.app.services.executable_pairs import (
    ExecutablePair,
    ExecutablePairInput,
    build_pair_fingerprints,
)


@pytest.mark.asyncio
async def test_executable_pair_review_survives_repository_recreation() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@localhost:5432/poly",
    )
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    pair = ExecutablePair(
        id=str(uuid4()),
        title="Persisted pair",
        kalshi_market_id="K-PERSIST",
        kalshi_outcome="no",
        kalshi_rule_text="Kalshi rule",
        kalshi_rule_url="https://kalshi.test/rule",
        polymarket_market_id="P-PERSIST",
        polymarket_outcome="yes",
        polymarket_rule_text="Polymarket rule",
        polymarket_rule_url="https://polymarket.test/rule",
        minimum_quantity=Decimal(1),
        quantity_step=Decimal(1),
        enabled=True,
        status=MappingStatus.EXACT,
        kalshi_expected_settlement_at=datetime(2026, 8, 24, 2, 0, tzinfo=UTC),
        polymarket_expected_settlement_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        worst_case_settlement_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        kalshi_category="politics",
        polymarket_category="news",
        kalshi_minimum_tick=Decimal("0.01"),
        polymarket_minimum_tick=Decimal("0.001"),
        native_fingerprint="sha256:native",
        material_fingerprint="sha256:material",
        checklist={"subject": True},
        truth_table=({"kalshi": Decimal(1), "polymarket": Decimal(0)},),
        reviewed_by="local-reviewer",
        source_candidate_id="oddpool-persisted",
        source_updated_at=datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS executable_pair (
                        id UUID PRIMARY KEY,
                        status VARCHAR(32) NOT NULL,
                        enabled BOOLEAN NOT NULL,
                        snapshot JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        await PostgresExecutablePairRepository(sessions).save(pair)
        restored = await PostgresExecutablePairRepository(sessions).get(pair.id)

        assert restored == pair
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM executable_pair WHERE id = CAST(:id AS uuid)"),
                {"id": pair.id},
            )
        await engine.dispose()


def test_pair_fingerprints_normalize_offset_equivalent_datetimes() -> None:
    utc = ExecutablePairInput(
        title="Pair",
        kalshi_market_id="K-PAIR",
        kalshi_outcome="no",
        kalshi_rule_text="Kalshi rule",
        kalshi_rule_url="https://kalshi.test/rule",
        polymarket_market_id="P-PAIR",
        polymarket_outcome="yes",
        polymarket_rule_text="Polymarket rule",
        polymarket_rule_url="https://polymarket.test/rule",
        minimum_quantity=Decimal(1),
        quantity_step=Decimal(1),
        enabled=True,
        kalshi_expected_settlement_at=datetime(2026, 8, 25, 15, 0, tzinfo=UTC),
        polymarket_expected_settlement_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        worst_case_settlement_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        kalshi_category="politics",
        polymarket_category="news",
        kalshi_minimum_tick=Decimal("0.01"),
        polymarket_minimum_tick=Decimal("0.001"),
    )
    offset = ExecutablePairInput(
        title="Pair",
        kalshi_market_id="K-PAIR",
        kalshi_outcome="no",
        kalshi_rule_text="Kalshi rule",
        kalshi_rule_url="https://kalshi.test/rule",
        polymarket_market_id="P-PAIR",
        polymarket_outcome="yes",
        polymarket_rule_text="Polymarket rule",
        polymarket_rule_url="https://polymarket.test/rule",
        minimum_quantity=Decimal(1),
        quantity_step=Decimal(1),
        enabled=True,
        kalshi_expected_settlement_at=datetime.fromisoformat("2026-08-25T23:00:00+08:00"),
        polymarket_expected_settlement_at=datetime.fromisoformat("2026-08-26T10:00:00+08:00"),
        worst_case_settlement_at=datetime.fromisoformat("2026-08-26T10:00:00+08:00"),
        kalshi_category="politics",
        polymarket_category="news",
        kalshi_minimum_tick=Decimal("0.01"),
        polymarket_minimum_tick=Decimal("0.001"),
    )

    assert build_pair_fingerprints(utc) == build_pair_fingerprints(offset)


def test_record_parses_legacy_snapshot_defaults_and_string_false() -> None:
    restored = _record(
        {
            "id": str(uuid4()),
            "title": "Legacy pair",
            "kalshi_market_id": "K-LEGACY",
            "kalshi_outcome": "no",
            "kalshi_rule_text": "Kalshi rule",
            "kalshi_rule_url": "https://kalshi.test/rule",
            "polymarket_market_id": "P-LEGACY",
            "polymarket_outcome": "yes",
            "polymarket_rule_text": "Polymarket rule",
            "polymarket_rule_url": "https://polymarket.test/rule",
            "minimum_quantity": "1",
            "quantity_step": "1",
            "enabled": "false",
            "status": "exact",
            "checklist": {"subject": "false"},
            "truth_table": [{"kalshi": "1", "polymarket": "0"}],
            "notes": "",
            "reviewed_by": None,
            "source_candidate_id": None,
            "source_updated_at": "2026-08-21T10:00:00+08:00",
        }
    )

    assert restored.enabled is False
    assert restored.checklist == {"subject": False}
    assert restored.kalshi_expected_settlement_at is None
    assert restored.polymarket_expected_settlement_at is None
    assert restored.worst_case_settlement_at is None
    assert restored.kalshi_category == ""
    assert restored.polymarket_category == ""
    assert restored.kalshi_minimum_tick == Decimal(0)
    assert restored.polymarket_minimum_tick == Decimal(0)
    assert restored.native_fingerprint == ""
    assert restored.material_fingerprint == ""
    assert restored.source_updated_at == datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
