import io
from types import SimpleNamespace

import pytest

from backend.app.core.secrets import InMemorySecretStore
from backend.app.desktop import __main__ as desktop_main


@pytest.mark.asyncio
async def test_mac_diagnostic_uses_same_noninteractive_store(monkeypatch) -> None:
    store = InMemorySecretStore()
    selected = []

    def no_ui_factory(service):
        selected.append(service)
        return store

    def legacy_factory(service):
        raise AssertionError('legacy keychain store must not be used on Mac')

    monkeypatch.setattr(desktop_main, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(desktop_main, 'MacOSNoUISecretStore', no_ui_factory, raising=False)
    monkeypatch.setattr(desktop_main, 'KeyringSecretStore', legacy_factory)
    result = await desktop_main.keychain_smoke(
        'set', 'ci-smoke-no-ui', secret_stream=io.BytesIO(b'synthetic-fixture')
    )
    assert result.fields == {'keychain_smoke': 'set'}
    assert selected == ['com.poly.desktop.integrations']
