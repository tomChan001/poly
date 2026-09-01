from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AnyHttpUrl, BaseModel, SecretStr

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.secrets import SecretStorageError
from backend.app.core.security import Principal, Role, require_role
from backend.app.services.integration_config import (
    ConnectionTestResult,
    IntegrationConfigView,
    IntegrationEnvironment,
    IntegrationProvider,
    SecretStatus,
)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

_CREDENTIAL_STORAGE_UNAVAILABLE = "credential storage unavailable"


class IntegrationUpdateRequest(BaseModel):
    enabled: bool
    environment: IntegrationEnvironment
    base_url: AnyHttpUrl
    configuration: dict[str, str | int | bool]
    secrets: dict[str, SecretStr | None]


@router.get("", response_model=list[IntegrationConfigView])
async def list_integrations(
    container: Annotated[ApplicationContainer, Depends(get_container)],
    _principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> list[IntegrationConfigView]:
    try:
        return await container.integration_configs.list()
    except SecretStorageError:
        raise HTTPException(
            status_code=503,
            detail=_CREDENTIAL_STORAGE_UNAVAILABLE,
        ) from None


@router.put("/{provider}", response_model=IntegrationConfigView)
async def update_integration(
    provider: IntegrationProvider,
    payload: IntegrationUpdateRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> IntegrationConfigView:
    try:
        return await container.integration_configs.update(
            provider,
            enabled=payload.enabled,
            environment=payload.environment,
            base_url=str(payload.base_url),
            configuration=payload.configuration,
            secrets={
                name: value.get_secret_value() if value is not None else None
                for name, value in payload.secrets.items()
            },
            actor=principal.subject,
        )
    except SecretStorageError:
        raise HTTPException(
            status_code=503,
            detail=_CREDENTIAL_STORAGE_UNAVAILABLE,
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{provider}/test", response_model=ConnectionTestResult)
async def test_integration_connection(
    provider: IntegrationProvider,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    _principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> ConnectionTestResult:
    try:
        return await container.integration_configs.test_connection(provider)
    except SecretStorageError:
        raise HTTPException(
            status_code=503,
            detail=_CREDENTIAL_STORAGE_UNAVAILABLE,
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{provider}/secrets/{name}", response_model=SecretStatus)
async def delete_integration_secret(
    provider: IntegrationProvider,
    name: str,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    _principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> SecretStatus:
    try:
        return await container.integration_configs.delete_secret(provider, name)
    except SecretStorageError:
        raise HTTPException(
            status_code=503,
            detail=_CREDENTIAL_STORAGE_UNAVAILABLE,
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
