import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services.integration_config import (
    IntegrationConfigRecord,
    IntegrationEnvironment,
    IntegrationProvider,
)


class PostgresIntegrationConfigRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list(self) -> list[IntegrationConfigRecord]:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT * FROM integration_config ORDER BY provider")
            )
            return [self._record(row._mapping) for row in result]

    async def get(self, provider: IntegrationProvider) -> IntegrationConfigRecord | None:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT * FROM integration_config WHERE provider = :provider"),
                {"provider": provider.value},
            )
            row = result.first()
            return None if row is None else self._record(row._mapping)

    async def upsert(self, record: IntegrationConfigRecord) -> IntegrationConfigRecord:
        async with self._sessions.begin() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO integration_config (
                        provider, enabled, environment, base_url, configuration,
                        version, updated_at, updated_by
                    ) VALUES (
                        :provider, :enabled, :environment, :base_url,
                        CAST(:configuration AS jsonb), 1, :updated_at, :updated_by
                    )
                    ON CONFLICT (provider) DO UPDATE SET
                        enabled = EXCLUDED.enabled,
                        environment = EXCLUDED.environment,
                        base_url = EXCLUDED.base_url,
                        configuration = EXCLUDED.configuration,
                        version = integration_config.version + 1,
                        updated_at = EXCLUDED.updated_at,
                        updated_by = EXCLUDED.updated_by
                    RETURNING *
                    """
                ),
                {
                    "provider": record.provider.value,
                    "enabled": record.enabled,
                    "environment": record.environment.value,
                    "base_url": record.base_url,
                    "configuration": json.dumps(record.configuration),
                    "updated_at": record.updated_at,
                    "updated_by": record.updated_by,
                },
            )
            return self._record(result.one()._mapping)

    @staticmethod
    def _record(row) -> IntegrationConfigRecord:
        return IntegrationConfigRecord(
            provider=IntegrationProvider(row["provider"]),
            enabled=row["enabled"],
            environment=IntegrationEnvironment(row["environment"]),
            base_url=row["base_url"],
            configuration=dict(row["configuration"]),
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
        )
