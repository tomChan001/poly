import secrets
from dataclasses import dataclass, field
from hmac import compare_digest
from urllib.parse import urlsplit

from fastapi import Request

CAPABILITY_HEADER = "x-poly-desktop-session"


@dataclass(slots=True)
class DesktopSession:
    _bootstrap_token: str = field(repr=False)
    _capability_value: str = field(repr=False)
    _bootstrap_used: bool = field(default=False, init=False)
    _port: int | None = field(default=None, init=False, repr=False)

    @classmethod
    def create(cls) -> "DesktopSession":
        return cls(secrets.token_urlsafe(32), secrets.token_urlsafe(32))

    @property
    def bootstrap_path(self) -> str:
        if self._bootstrap_used:
            raise RuntimeError("desktop bootstrap already used")
        return f"/desktop/bootstrap/{self._bootstrap_token}"

    def exchange(self, supplied: str) -> str | None:
        if self._bootstrap_used or not compare_digest(supplied, self._bootstrap_token):
            return None
        self._bootstrap_used = True
        self._bootstrap_token = ""
        return self._capability_value

    def bind_port(self, port: int) -> None:
        if self._port is not None or not 1 <= port <= 65535:
            raise RuntimeError("desktop session authority is already bound")
        self._port = port

    def has_trusted_authority(self, request: Request) -> bool:
        if self._port is None:
            return False
        expected = f"127.0.0.1:{self._port}"
        if request.headers.get("host") != expected:
            return False
        origin = request.headers.get("origin")
        if origin is None:
            return True
        try:
            parsed = urlsplit(origin)
            return (
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and parsed.port == self._port
                and parsed.path == ""
                and parsed.username is None
                and parsed.password is None
                and parsed.query == ""
                and parsed.fragment == ""
            )
        except ValueError:
            return False

    def is_authorized(self, request: Request) -> bool:
        supplied = request.headers.get(CAPABILITY_HEADER, "")
        return (
            self.has_trusted_authority(request)
            and bool(supplied)
            and compare_digest(supplied, self._capability_value)
        )
