# Windows Keyring Chunking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make standard Kalshi RSA PEM private keys save reliably in Windows Credential Manager without changing the integration API or exposing secret material.

**Architecture:** Extend `KeyringSecretStore` with a backwards-compatible versioned sidecar manifest. Values up to 900 characters remain verbatim single credentials at the original key; longer values are written as digest-namespaced chunks before an atomic sidecar-manifest switch, then reconstructed and SHA-256 verified on read. Keeping metadata on a separate key prevents even manifest-looking short literals from colliding with the internal format. Convert storage failures to a dedicated sanitized exception that the integration API maps to HTTP 503.

> **Final implementation note:** The initial step examples below placed the manifest in the base key. Review found that this made a short literal identical to a valid manifest ambiguous. The implemented format therefore stores metadata at `<key>:manifest:v1`; the base key remains exclusively for verbatim short values. Long-secret deletion removes any stale base value, then the sidecar, then retries chunk cleanup so a failure cannot leave an active manifest pointing at missing chunks.

**Tech Stack:** Python 3.12, `keyring` WinVault backend, `asyncio.to_thread`, SHA-256, JSON, FastAPI, pytest, Ruff.

---

## File map

- Create `backend/tests/unit/core/test_secrets.py`: boundary, round-trip, replacement, deletion, corruption, and partial-write tests for the keyring adapter.
- Modify `backend/app/core/secrets.py`: versioned chunk manifest, safe reconstruction, cleanup, and sanitized storage exception.
- Modify `backend/tests/integration/api/test_integrations.py`: API regression for a credential-store failure.
- Modify `backend/app/api/routes/integrations.py`: translate credential-store failures into a safe HTTP 503 response.

### Task 1: Reproduce and fix long-secret storage

**Files:**
- Create: `backend/tests/unit/core/test_secrets.py`
- Modify: `backend/app/core/secrets.py`

- [ ] **Step 1: Write the failing long-PEM round-trip test**

Create an in-memory keyring double that enforces the measured Windows limit and patch the real `keyring` module used by `KeyringSecretStore`:

```python
import keyring
import pytest
from hashlib import sha256

from backend.app.core.secrets import KeyringSecretStore


class LimitedKeyring:
    def __init__(
        self,
        limit: int = 1280,
        fail_on_write_number: int | None = None,
    ) -> None:
        self.limit = limit
        self.fail_on_write_number = fail_on_write_number
        self.write_count = 0
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.values.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.write_count += 1
        if self.write_count == self.fail_on_write_number:
            raise OSError(1783, "CredWrite")
        if len(value) > self.limit:
            raise OSError(1783, "CredWrite")
        self.values[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        self.values.pop((service, key), None)


def install_keyring(monkeypatch, backend: LimitedKeyring) -> None:
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)


@pytest.mark.asyncio
async def test_long_pem_round_trips_through_limited_keyring(monkeypatch) -> None:
    backend = LimitedKeyring()
    install_keyring(monkeypatch, backend)
    store = KeyringSecretStore("test-service")
    pem = "-----BEGIN PRIVATE KEY-----\n" + ("a" * 1650) + "\n-----END PRIVATE KEY-----\n"

    await store.set("kalshi:private_key", pem)

    assert await store.get("kalshi:private_key") == pem
    assert all(len(value) <= backend.limit for value in backend.values.values())
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
uv run pytest backend/tests/unit/core/test_secrets.py::test_long_pem_round_trips_through_limited_keyring -q
```

Expected: FAIL with `OSError: [Errno 1783] CredWrite` because the current adapter sends the entire PEM as one credential.

- [ ] **Step 3: Add replacement, deletion, corruption, and failed-write tests**

Add tests using the same real `KeyringSecretStore` and fake backend:

```python
from backend.app.core.secrets import SecretStorageError


@pytest.mark.asyncio
async def test_short_secret_keeps_legacy_single_entry(monkeypatch) -> None:
    backend = LimitedKeyring()
    install_keyring(monkeypatch, backend)
    store = KeyringSecretStore("test-service")

    await store.set("oddpool:api_token", "short-token")

    assert backend.values == {("test-service", "oddpool:api_token"): "short-token"}
    assert await store.get("oddpool:api_token") == "short-token"


@pytest.mark.asyncio
async def test_replacing_long_secret_removes_old_chunks(monkeypatch) -> None:
    backend = LimitedKeyring()
    install_keyring(monkeypatch, backend)
    store = KeyringSecretStore("test-service")
    first = "x" * 1800
    second = "y" * 1800
    first_digest = sha256(first.encode("utf-8")).hexdigest()

    await store.set("kalshi:private_key", first)
    await store.set("kalshi:private_key", second)

    assert await store.get("kalshi:private_key") == second
    assert not any(first_digest in key for _, key in backend.values)


@pytest.mark.asyncio
async def test_replacing_long_secret_with_short_value_removes_chunks(monkeypatch) -> None:
    backend = LimitedKeyring()
    install_keyring(monkeypatch, backend)
    store = KeyringSecretStore("test-service")

    await store.set("kalshi:private_key", "x" * 1800)
    await store.set("kalshi:private_key", "short")

    assert await store.get("kalshi:private_key") == "short"
    assert backend.values == {("test-service", "kalshi:private_key"): "short"}


@pytest.mark.asyncio
async def test_delete_removes_manifest_and_all_chunks(monkeypatch) -> None:
    backend = LimitedKeyring()
    install_keyring(monkeypatch, backend)
    store = KeyringSecretStore("test-service")

    await store.set("kalshi:private_key", "x" * 1800)
    await store.delete("kalshi:private_key")

    assert backend.values == {}
    assert await store.get("kalshi:private_key") is None


@pytest.mark.asyncio
async def test_missing_chunk_fails_closed(monkeypatch) -> None:
    backend = LimitedKeyring()
    install_keyring(monkeypatch, backend)
    store = KeyringSecretStore("test-service")

    await store.set("kalshi:private_key", "x" * 1800)
    chunk_key = next(key for service, key in backend.values if ":chunk:" in key)
    backend.values.pop(("test-service", chunk_key))

    with pytest.raises(SecretStorageError, match="credential storage is corrupted"):
        await store.get("kalshi:private_key")


@pytest.mark.asyncio
async def test_failed_chunk_write_keeps_previous_value(monkeypatch) -> None:
    backend = LimitedKeyring()
    install_keyring(monkeypatch, backend)
    store = KeyringSecretStore("test-service")
    previous = "x" * 1800

    await store.set("kalshi:private_key", previous)
    previous_entries = dict(backend.values)
    backend.fail_on_write_number = backend.write_count + 2

    with pytest.raises(SecretStorageError, match="credential storage write failed"):
        await store.set("kalshi:private_key", "y" * 1800)

    backend.fail_on_write_number = None
    assert backend.values == previous_entries
    assert await store.get("kalshi:private_key") == previous
```

These tests require the implementation to remove partial new chunks and preserve the prior manifest when any chunk write fails.

- [ ] **Step 4: Implement the minimal versioned chunk format**

In `backend/app/core/secrets.py`, add:

```python
import json
from dataclasses import dataclass
from hashlib import sha256

_CHUNK_SIZE = 900
_MANIFEST_PREFIX = "poly-keyring-chunks:v1:"


class SecretStorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ChunkManifest:
    digest: str
    count: int

    def encode(self) -> str:
        return _MANIFEST_PREFIX + json.dumps(
            {"sha256": self.digest, "chunks": self.count},
            separators=(",", ":"),
            sort_keys=True,
        )


def _parse_manifest(value: str) -> _ChunkManifest | None:
    if not value.startswith(_MANIFEST_PREFIX):
        return None
    try:
        payload = json.loads(value.removeprefix(_MANIFEST_PREFIX))
        digest = payload["sha256"]
        count = payload["chunks"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError
        int(digest, 16)
        if not isinstance(count, int) or not 1 <= count <= 10_000:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SecretStorageError("credential storage is corrupted") from exc
    return _ChunkManifest(digest, count)
```

Add private helpers on `KeyringSecretStore` for raw get/set/delete and chunk key construction:

```python
@staticmethod
def _chunk_key(key: str, digest: str, index: int) -> str:
    return f"{key}:chunk:{digest}:{index}"
```

Implement `get` so it returns legacy values directly, or loads every referenced chunk, concatenates them, and verifies SHA-256 before returning. Implement `set` so values of at most `_CHUNK_SIZE` use the legacy entry; longer values write digest-namespaced chunks first and the manifest last. On any exception, delete newly written chunks and raise `SecretStorageError("credential storage write failed")` without including the value or raw exception text. After a successful switch, clean up chunks referenced by the old manifest. Implement `delete` by deleting the main entry and then any referenced chunks while keeping missing deletes idempotent.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
uv run pytest backend/tests/unit/core/test_secrets.py -q
uv run ruff check backend/app/core/secrets.py backend/tests/unit/core/test_secrets.py
```

Expected: all new secret-store tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 6: Commit the storage fix**

```powershell
git add backend/app/core/secrets.py backend/tests/unit/core/test_secrets.py
git commit -m "fix: chunk long windows keyring secrets"
```

### Task 2: Return a safe API error when credential storage fails

**Files:**
- Modify: `backend/tests/integration/api/test_integrations.py`
- Modify: `backend/app/api/routes/integrations.py`

- [ ] **Step 1: Write the failing API test**

Add a failing secret store and install it into the existing test container:

```python
from backend.app.core.secrets import SecretStorageError


class FailingSecretStore:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str) -> None:
        raise SecretStorageError("must-not-leak-private-key")

    async def delete(self, key: str) -> None:
        return None


@pytest.mark.asyncio
async def test_integration_save_redacts_credential_store_failure() -> None:
    container = ApplicationContainer()
    container.integration_configs._secrets = FailingSecretStore()
    transport = httpx.ASGITransport(app=app_for(container))
    payload = {
        "enabled": True,
        "environment": "production",
        "base_url": "https://api.elections.kalshi.com",
        "configuration": {"key_id": "test-key-id"},
        "secrets": {"private_key": "must-not-leak-private-key"},
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/integrations/kalshi", json=payload)

    assert response.status_code == 503
    assert response.json() == {"detail": "credential storage unavailable"}
    assert "must-not-leak" not in response.text
```

- [ ] **Step 2: Run the API test and verify RED**

Run:

```powershell
uv run pytest backend/tests/integration/api/test_integrations.py::test_integration_save_redacts_credential_store_failure -q
```

Expected: FAIL because `SecretStorageError` currently escapes the route instead of becoming a 503 response.

- [ ] **Step 3: Map the storage exception to a sanitized response**

In `backend/app/api/routes/integrations.py`, import `SecretStorageError` and add a dedicated handler before the existing `ValueError` handler:

```python
    except SecretStorageError as exc:
        raise HTTPException(
            status_code=503,
            detail="credential storage unavailable",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
```

Apply the same sanitized handler to secret deletion because it uses the same keyring adapter. Connection testing only reads credentials, so map `SecretStorageError` there as well.

- [ ] **Step 4: Run API tests and verify GREEN**

Run:

```powershell
uv run pytest backend/tests/integration/api/test_integrations.py -q
uv run ruff check backend/app/api/routes/integrations.py backend/tests/integration/api/test_integrations.py
```

Expected: all integration API tests pass and no response contains the sentinel secret.

- [ ] **Step 5: Commit the API behavior**

```powershell
git add backend/app/api/routes/integrations.py backend/tests/integration/api/test_integrations.py
git commit -m "fix: report credential storage failures safely"
```

### Task 3: Verify Windows behavior and complete regression checks

**Files:**
- Verify only; no production file changes expected.

- [ ] **Step 1: Exercise the real Windows Credential Manager safely**

Generate a temporary RSA key in memory, save it under `_diagnostic:kalshi-private-key`, read it back, compare equality, and delete it in `finally`. Print only key length and equality, never PEM content:

```powershell
@'
import asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from backend.app.core.config import settings
from backend.app.core.secrets import KeyringSecretStore

async def main():
    store = KeyringSecretStore(settings.credential_service_name)
    key = "_diagnostic:kalshi-private-key"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    try:
        await store.set(key, pem)
        restored = await store.get(key)
        print(f"length={len(pem)} round_trip={restored == pem}")
    finally:
        await store.delete(key)

asyncio.run(main())
'@ | uv run python -
```

Expected: `round_trip=True`; the diagnostic key and all chunks are removed afterward.

- [ ] **Step 2: Run backend regression checks**

Run against the currently published local PostgreSQL container without printing its password:

```powershell
uv run pytest -q
uv run ruff check backend migrations
uv run mypy backend/app/core/secrets.py backend/app/api/routes/integrations.py backend/tests/unit/core/test_secrets.py
```

Expected: pytest passes with only the existing destructive-database skip, Ruff passes, and the modified files have no Mypy errors.

- [ ] **Step 3: Run unchanged frontend regression checks**

```powershell
Set-Location frontend
npm run test -- --reporter=dot
npm run lint
npm run build
npm exec playwright test -- --reporter=line
```

Expected: 7 Vitest tests pass, lint and production build pass, and 8 Playwright viewport tests pass.

- [ ] **Step 4: Restart the backend from the implementation branch and verify health**

Start the API with the existing local database environment, `TRADING_MODE=read_only`, `OPENING_ENABLED=false`, and `LOCAL_SETUP_ENABLED=true`. Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/health
```

Expected: `status=ok`, `trading_mode=read_only`, and `opening_enabled=false`. Ask the operator to paste the original Kalshi PEM and retry Save; the API must no longer return 500.

- [ ] **Step 5: Confirm repository state**

```powershell
git diff --check
git status --short
git log -3 --oneline
```

Expected: no uncommitted implementation files and the two fix commits are present.
