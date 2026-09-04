import secrets
from dataclasses import dataclass, field
from hmac import compare_digest

from fastapi import Request

COOKIE_NAME = "poly_desktop_session"


@dataclass(slots=True)
class DesktopSession:
    _bootstrap_token: str
    _cookie_value: str
    _bootstrap_used: bool = field(default=False, init=False)
    _bootstrap_path: str = field(init=False)

    def __post_init__(self) -> None:
        self._bootstrap_path = f"/desktop/bootstrap/{self._bootstrap_token}"

    @classmethod
    def create(cls) -> "DesktopSession":
        return cls(secrets.token_urlsafe(32), secrets.token_urlsafe(32))

    @property
    def bootstrap_path(self) -> str:
        return self._bootstrap_path

    def exchange(self, supplied: str) -> str | None:
        if self._bootstrap_used or not compare_digest(supplied, self._bootstrap_token):
            return None
        self._bootstrap_used = True
        self._bootstrap_token = ""
        return self._cookie_value

    def is_authorized(self, request: Request) -> bool:
        supplied = request.cookies.get(COOKIE_NAME, "")
        return bool(supplied) and compare_digest(supplied, self._cookie_value)
