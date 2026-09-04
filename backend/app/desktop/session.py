import secrets
from dataclasses import dataclass, field
from hmac import compare_digest

from fastapi import Request

COOKIE_NAME = "poly_desktop_session"


@dataclass(slots=True)
class DesktopSession:
    _bootstrap_token: str = field(repr=False)
    _cookie_value: str = field(repr=False)
    _bootstrap_used: bool = field(default=False, init=False)

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
        return self._cookie_value

    def is_authorized(self, request: Request) -> bool:
        supplied = request.cookies.get(COOKIE_NAME, "")
        return bool(supplied) and compare_digest(supplied, self._cookie_value)
