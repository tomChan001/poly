from dataclasses import fields
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.config import settings
from backend.app.desktop.session import CAPABILITY_HEADER, DesktopSession
from backend.app.main import create_app

PORT = 49152
BASE_URL = f"http://127.0.0.1:{PORT}"


def bound_session() -> DesktopSession:
    session = DesktopSession.create()
    session.bind_port(PORT)
    return session


def response_capability(response: httpx.Response) -> str:
    fragment = urlsplit(response.headers["location"]).fragment
    return parse_qs(fragment)["poly_session"][0]


@pytest.mark.asyncio
async def test_desktop_api_requires_explicit_capability_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = bound_session()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    bootstrap_path = session.bootstrap_path
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
    ) as client:
        denied = await client.get("/api/opportunities")
        denied_health = await client.get("/health")
        bootstrap = await client.get(bootstrap_path, follow_redirects=False)
        capability = response_capability(bootstrap)
        allowed = await client.get(
            "/api/opportunities",
            headers={"Origin": BASE_URL, CAPABILITY_HEADER: capability},
        )
        allowed_health = await client.get(
            "/health", headers={CAPABILITY_HEADER: capability}
        )
        replay = await client.get(bootstrap_path, follow_redirects=False)

    assert denied.status_code == 403
    assert denied_health.status_code == 403
    assert bootstrap.status_code == 303
    assert bootstrap.headers["location"].startswith("/#poly_session=")
    assert "set-cookie" not in bootstrap.headers
    assert bootstrap.headers["cache-control"] == "no-store"
    assert allowed.status_code == 200
    assert allowed.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert allowed.headers["x-frame-options"] == "DENY"
    assert allowed_health.status_code == 200
    assert replay.status_code == 403


def test_desktop_session_repr_redacts_secrets_and_exchange_discards_token() -> None:
    session = DesktopSession.create()
    bootstrap_path = session.bootstrap_path
    bootstrap_token = bootstrap_path.rsplit("/", maxsplit=1)[-1]
    before_exchange = repr(session)

    capability = session.exchange(bootstrap_token)

    assert capability is not None
    assert bootstrap_token not in before_exchange
    assert capability not in before_exchange
    assert bootstrap_token not in repr(session)
    assert capability not in repr(session)
    with pytest.raises(RuntimeError, match="desktop bootstrap already used"):
        session.bootstrap_path.startswith("/desktop/")

    stored_string_values = [
        value
        for descriptor in fields(session)
        if isinstance(value := getattr(session, descriptor.name), str)
    ]
    assert bootstrap_path not in stored_string_values
    assert bootstrap_token not in stored_string_values


@pytest.mark.asyncio
async def test_hostile_origin_stays_blocked_after_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = bound_session()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
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
    session = bound_session()
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
    session = bound_session()
    bootstrap_path = session.bootstrap_path
    app = create_app(ApplicationContainer(), desktop_session=session)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))

    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
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
async def test_bootstrap_returns_nonambient_fragment_capability() -> None:
    session = bound_session()
    bootstrap_path = session.bootstrap_path
    token = bootstrap_path.rsplit("/", maxsplit=1)[-1]
    app = create_app(ApplicationContainer(), desktop_session=session)
    transport = httpx.ASGITransport(app=app, client=("::1", 51000))

    async with httpx.AsyncClient(
        transport=transport, base_url=BASE_URL
    ) as client:
        response = await client.get(bootstrap_path, follow_redirects=False)

    capability = response_capability(response)
    assert response.status_code == 303
    assert capability != token
    assert "set-cookie" not in response.headers
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


@pytest.mark.asyncio
async def test_desktop_health_rejects_bogus_capability_from_loopback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = bound_session()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))

    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
    ) as client:
        response = await client.get(
            "/health",
            headers={CAPABILITY_HEADER: "bogus"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "desktop session required"}


@pytest.mark.asyncio
async def test_desktop_health_rejects_valid_capability_from_remote_client(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = bound_session()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    bootstrap_path = session.bootstrap_path
    loopback_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(
        transport=loopback_transport,
        base_url=BASE_URL,
    ) as client:
        bootstrap = await client.get(bootstrap_path, follow_redirects=False)
    capability = response_capability(bootstrap)

    remote_transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 51000))
    async with httpx.AsyncClient(
        transport=remote_transport,
        base_url=BASE_URL,
    ) as client:
        response = await client.get(
            "/health",
            headers={CAPABILITY_HEADER: capability},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "local access only"}


@pytest.mark.asyncio
async def test_desktop_health_rejects_valid_capability_with_hostile_origin(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = bound_session()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    bootstrap_path = session.bootstrap_path
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
    ) as client:
        bootstrap = await client.get(bootstrap_path, follow_redirects=False)
        capability = response_capability(bootstrap)
        response = await client.get(
            "/health",
            headers={
                "Origin": "https://evil.example",
                CAPABILITY_HEADER: capability,
            },
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "local access only"}


@pytest.mark.asyncio
async def test_desktop_capability_is_bound_to_the_exact_loopback_port(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = bound_session()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        bootstrap = await client.get(session.bootstrap_path, follow_redirects=False)
    capability = response_capability(bootstrap)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:49153",
    ) as client:
        response = await client.get(
            "/health",
            headers={CAPABILITY_HEADER: capability},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "local access only"}


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
