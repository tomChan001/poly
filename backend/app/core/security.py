import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, status

REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "cookie",
    "privatekey",
    "secret",
    "signature",
    "token",
}


class Role(StrEnum):
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    roles: frozenset[Role]


def redact_sensitive(value: object) -> object:
    if isinstance(value, Mapping):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            redacted[key] = REDACTED if normalized in _SENSITIVE_KEYS else redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return value


def redact_text(message: str) -> str:
    message = re.sub(
        r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s]+",
        rf"\1{REDACTED}",
        message,
    )
    return re.sub(r"(?i)(cookie\s*:\s*)[^\s]+", rf"\1{REDACTED}", message)


def get_current_principal() -> Principal:
    # Production must replace this dependency with an OIDC validator configured
    # for the deployment issuer and audience. Missing auth fails closed.
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="OIDC verifier is not configured",
    )


def require_authenticated(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    return principal


def require_role(required: Role) -> Callable[..., Principal]:
    def dependency(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        if required not in principal.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{required.value} role required",
            )
        return principal

    return dependency
