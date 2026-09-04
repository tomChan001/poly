import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from backend.app.core.config import settings

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
            redacted[key] = (
                REDACTED if normalized in _SENSITIVE_KEYS else redact_sensitive(item)
            )
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


def get_current_principal(request: Request) -> Principal:
    # Local setup is restricted to the machine running this desktop service.
    # The loopback check keeps remote callers out while allowing the local web
    # configuration screen to work when real trading is the configured mode.
    if is_local_setup_request(request):
        desktop_session = getattr(request.app.state, "desktop_session", None)
        if desktop_session is not None and not desktop_session.is_authorized(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="desktop session required",
            )
        # The local operator is also the only human reviewer in this
        # single-machine deployment; order execution itself remains automatic.
        return Principal("local-setup", frozenset(Role))

    # This is a single-machine control plane. There is deliberately no remote
    # login fallback: callers outside the local process boundary are rejected.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="local access only",
    )


def is_local_setup_request(request: Request) -> bool:
    configured_settings = getattr(request.app.state, "settings", settings)
    return bool(
        configured_settings.local_setup_enabled
        and is_loopback_request(request)
        and _has_trusted_local_origin(request)
        and request.app.state.allow_local_setup
    )


def is_loopback_request(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return False


def _has_trusted_local_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        # Native local tools do not send Origin. Browser requests do, which
        # lets this boundary reject cross-site requests targeting localhost.
        return True
    try:
        host = urlsplit(origin).hostname
        return host == "localhost" or (
            host is not None and ip_address(host).is_loopback
        )
    except ValueError:
        return False


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
