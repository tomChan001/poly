import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.config import settings
from backend.app.desktop.session import COOKIE_NAME, DesktopSession
from backend.app.main import create_app


@pytest.mark.asyncio
async def test_desktop_api_requires_capability_cookie(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = DesktopSession.create()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        denied = await client.get("/api/opportunities")
        denied_health = await client.get("/health")
        bootstrap = await client.get(session.bootstrap_path, follow_redirects=False)
        allowed = await client.get(
            "/api/opportunities",
            headers={"Origin": "http://127.0.0.1:49152"},
        )
        allowed_health = await client.get("/health")
        replay = await client.get(session.bootstrap_path, follow_redirects=False)

    assert denied.status_code == 403
    assert denied_health.status_code == 403
    assert bootstrap.status_code == 303
    assert bootstrap.headers["location"] == "/"
    assert "HttpOnly" in bootstrap.headers["set-cookie"]
    assert "SameSite=strict" in bootstrap.headers["set-cookie"]
    assert allowed.status_code == 200
    assert allowed_health.status_code == 200
    assert replay.status_code == 403


@pytest.mark.asyncio
async def test_hostile_origin_stays_blocked_after_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = DesktopSession.create()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        await client.get(session.bootstrap_path)
        response = await client.get(
            "/api/opportunities",
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "local access only"}


@pytest.mark.asyncio
async def test_bootstrap_rejects_non_loopback_client() -> None:
    session = DesktopSession.create()
    app = create_app(ApplicationContainer(), desktop_session=session)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 51000))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost"
    ) as client:
        response = await client.get(session.bootstrap_path, follow_redirects=False)

    assert response.status_code == 403
    assert response.json() == {"detail": "local access only"}


@pytest.mark.asyncio
async def test_bootstrap_rejects_wrong_token_without_consuming_session() -> None:
    session = DesktopSession.create()
    bootstrap_path = session.bootstrap_path
    app = create_app(ApplicationContainer(), desktop_session=session)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        wrong = await client.get(
            "/desktop/bootstrap/not-the-token",
            follow_redirects=False,
        )
        valid = await client.get(bootstrap_path, follow_redirects=False)

    assert wrong.status_code == 403
    assert wrong.json() == {"detail": "invalid desktop bootstrap"}
    assert valid.status_code == 303


@pytest.mark.asyncio
async def test_bootstrap_cookie_flags_and_value_are_not_the_url_token() -> None:
    session = DesktopSession.create()
    bootstrap_path = session.bootstrap_path
    token = bootstrap_path.rsplit("/", maxsplit=1)[-1]
    app = create_app(ApplicationContainer(), desktop_session=session)
    transport = httpx.ASGITransport(app=app, client=("::1", 51000))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://[::1]"
    ) as client:
        response = await client.get(bootstrap_path, follow_redirects=False)

    set_cookie = response.headers["set-cookie"]
    cookie_value = response.cookies[COOKIE_NAME]
    assert response.status_code == 303
    assert cookie_value != token
    assert token not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Secure" not in set_cookie


@pytest.mark.asyncio
async def test_non_desktop_behavior_does_not_require_cookie(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    app = create_app(ApplicationContainer(), allow_local_setup=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        api_response = await client.get("/api/opportunities")
        health_response = await client.get("/health")
        bootstrap_response = await client.get(
            "/desktop/bootstrap/not-configured",
            follow_redirects=False,
        )

    assert api_response.status_code == 200
    assert health_response.status_code == 200
    assert bootstrap_response.status_code == 404


@pytest.mark.asyncio
async def test_non_desktop_bootstrap_path_is_not_exposed_to_remote_client() -> None:
    app = create_app(ApplicationContainer())
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 51000))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost"
    ) as client:
        response = await client.get(
            "/desktop/bootstrap/not-configured",
            follow_redirects=False,
        )

    assert response.status_code == 404
