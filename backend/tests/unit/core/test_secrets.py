import json
import logging
from collections.abc import Callable

import keyring
import pytest
from keyring.errors import PasswordDeleteError

from backend.app.core.secrets import KeyringSecretStore, SecretStorageError

SERVICE = "poly-test"
MANIFEST_PREFIX = "poly-keyring-chunks:v1:"
MANIFEST_KIND = "chunked-secret"
INVALID_TYPED_MANIFESTS: tuple[dict[str, object], ...] = (
    {"kind": MANIFEST_KIND, "chunks": 1},
    {"kind": MANIFEST_KIND, "sha256": "a" * 64, "chunks": True},
    {"kind": MANIFEST_KIND, "sha256": "invalid", "chunks": 1},
)


class LimitedKeyring:
    def __init__(
        self,
        *,
        max_value_length: int = 1280,
        fail_on_set_call: int | None = None,
    ) -> None:
        self.max_value_length = max_value_length
        self.fail_on_set_call = fail_on_set_call
        self.set_calls = 0
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.entries.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.set_calls += 1
        if self.set_calls == self.fail_on_set_call:
            raise OSError(1783, "simulated credential write failure")
        if len(value) > self.max_value_length:
            raise OSError(1783, "The stub received bad data")
        self.entries[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        try:
            del self.entries[(service, key)]
        except KeyError as exc:
            raise PasswordDeleteError("credential does not exist") from exc


@pytest.fixture
def limited_keyring(monkeypatch: pytest.MonkeyPatch) -> LimitedKeyring:
    backend = LimitedKeyring()
    patches: tuple[tuple[str, Callable[..., object]], ...] = (
        ("get_password", backend.get_password),
        ("set_password", backend.set_password),
        ("delete_password", backend.delete_password),
    )
    for name, implementation in patches:
        monkeypatch.setattr(keyring, name, implementation)
    return backend


def test_limited_keyring_reproduces_windows_error_for_long_pem() -> None:
    backend = LimitedKeyring()
    pem = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 1640) + "\n-----END PRIVATE KEY-----"

    with pytest.raises(OSError) as caught:
        backend.set_password(SERVICE, "signing-key", pem)

    assert caught.value.errno == 1783


@pytest.mark.asyncio
async def test_long_pem_round_trips_through_limited_keyring(
    limited_keyring: LimitedKeyring,
) -> None:
    pem = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 1640) + "\n-----END PRIVATE KEY-----"
    store = KeyringSecretStore(SERVICE)

    await store.set("signing-key", pem)

    assert await store.get("signing-key") == pem
    chunk_values = [
        value
        for (service, key), value in limited_keyring.entries.items()
        if service == SERVICE and key.startswith("signing-key:chunk:")
    ]
    assert chunk_values
    assert all(0 < len(chunk) <= 900 for chunk in chunk_values)
    assert (SERVICE, "signing-key") not in limited_keyring.entries
    manifest = limited_keyring.entries[(SERVICE, _manifest_key("signing-key"))]
    assert manifest.startswith(MANIFEST_PREFIX)
    assert " " not in manifest.removeprefix(MANIFEST_PREFIX)
    assert json.loads(manifest.removeprefix(MANIFEST_PREFIX))["kind"] == MANIFEST_KIND


@pytest.mark.asyncio
async def test_short_value_remains_in_one_legacy_entry(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)

    await store.set("api-token", "short-secret")

    assert limited_keyring.entries == {(SERVICE, "api-token"): "short-secret"}
    assert await store.get("api-token") == "short-secret"


@pytest.mark.asyncio
async def test_valid_typed_manifest_literal_stays_verbatim_in_base_key(
    limited_keyring: LimitedKeyring,
) -> None:
    value = MANIFEST_PREFIX + json.dumps(
        {"kind": MANIFEST_KIND, "sha256": "a" * 64, "chunks": 1},
        separators=(",", ":"),
    )
    store = KeyringSecretStore(SERVICE)

    await store.set("credential", value)

    assert limited_keyring.entries == {(SERVICE, "credential"): value}
    assert await store.get("credential") == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "poly-keyring-chunks:v1:not-json",
        'poly-keyring-chunks:v1:{"purpose":"literal credential value"}',
    ],
)
async def test_untyped_manifest_prefix_short_values_stay_raw(
    limited_keyring: LimitedKeyring,
    value: str,
) -> None:
    store = KeyringSecretStore(SERVICE)

    await store.set("credential", value)

    assert limited_keyring.entries[(SERVICE, "credential")] == value
    assert await store.get("credential") == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "poly-keyring-chunks:v1:not-json",
        'poly-keyring-chunks:v1:{"purpose":"legacy literal"}',
    ],
)
async def test_legacy_manifest_prefix_literals_are_read_raw(
    limited_keyring: LimitedKeyring,
    value: str,
) -> None:
    limited_keyring.entries[(SERVICE, "credential")] = value
    store = KeyringSecretStore(SERVICE)

    assert await store.get("credential") == value


@pytest.mark.asyncio
async def test_plain_prefix_short_value_stays_raw(
    limited_keyring: LimitedKeyring,
) -> None:
    value = "poly-keyring-plain:v1:legacy literal"
    store = KeyringSecretStore(SERVICE)

    await store.set("credential", value)

    assert limited_keyring.entries[(SERVICE, "credential")] == value
    assert await store.get("credential") == value


@pytest.mark.asyncio
async def test_replacing_long_value_removes_old_chunks(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)
    old_chunk_keys = _chunk_keys(limited_keyring, "credential")

    await store.set("credential", "B" * 1900)

    assert await store.get("credential") == "B" * 1900
    assert old_chunk_keys.isdisjoint(_entry_keys(limited_keyring))


@pytest.mark.asyncio
async def test_replacing_long_value_with_short_removes_old_chunks(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)

    await store.set("credential", "replacement")

    assert limited_keyring.entries == {(SERVICE, "credential"): "replacement"}


@pytest.mark.asyncio
async def test_replacing_short_value_with_long_commits_sidecar_and_removes_base(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "short-value")

    await store.set("credential", "A" * 1900)

    assert (SERVICE, "credential") not in limited_keyring.entries
    assert (SERVICE, _manifest_key("credential")) in limited_keyring.entries
    assert await store.get("credential") == "A" * 1900


@pytest.mark.asyncio
async def test_short_to_long_sidecar_write_failure_preserves_short_value(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "short-value")
    original_entries = limited_keyring.entries.copy()
    limited_keyring.fail_on_set_call = limited_keyring.set_calls + 4

    with pytest.raises(SecretStorageError):
        await store.set("credential", "A" * 1900)

    assert await store.get("credential") == "short-value"
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_long_to_short_sidecar_delete_failure_preserves_long_value(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = KeyringSecretStore(SERVICE)
    old_value = "A" * 1900
    await store.set("credential", old_value)
    original_entries = limited_keyring.entries.copy()

    def fail_sidecar_delete(service: str, key: str) -> None:
        if service == SERVICE and key == _manifest_key("credential"):
            raise RuntimeError("private sidecar delete failure")
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_sidecar_delete)

    with pytest.raises(SecretStorageError):
        await store.set("credential", "replacement")

    assert await store.get("credential") == old_value
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["B" * 1900, "replacement"])
async def test_committed_replacement_does_not_fail_when_old_chunk_cleanup_fails(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    replacement: str,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)
    old_chunk_keys = _chunk_keys(limited_keyring, "credential")
    bottom_message = "backend failed deleting private old credential"

    def fail_old_chunk_delete(service: str, key: str) -> None:
        if service == SERVICE and key in old_chunk_keys:
            raise RuntimeError(bottom_message)
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_old_chunk_delete)

    with caplog.at_level(logging.WARNING):
        await store.set("credential", replacement)

    assert await store.get("credential") == replacement
    assert "credential storage cleanup failed" in caplog.text
    assert bottom_message not in caplog.text


@pytest.mark.asyncio
async def test_delete_removes_manifest_and_all_chunks(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)

    await store.delete("credential")
    await store.delete("credential")

    assert limited_keyring.entries == {}
    assert await store.get("credential") is None


@pytest.mark.asyncio
async def test_delete_can_retry_after_transient_chunk_delete_failure(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)
    failure_pending = True

    def fail_one_chunk_delete(service: str, key: str) -> None:
        nonlocal failure_pending
        if failure_pending and ":chunk:" in key:
            failure_pending = False
            raise RuntimeError("transient credential delete failure")
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_one_chunk_delete)

    await store.delete("credential")

    assert limited_keyring.entries == {}
    assert await store.get("credential") is None


@pytest.mark.asyncio
async def test_delete_base_failure_keeps_long_value_active(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = KeyringSecretStore(SERVICE)
    value = "A" * 1900
    await store.set("credential", value)
    original_entries = limited_keyring.entries.copy()

    def fail_base_delete(service: str, key: str) -> None:
        if service == SERVICE and key == "credential":
            raise RuntimeError("private base delete failure")
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_base_delete)

    with pytest.raises(SecretStorageError):
        await store.delete("credential")

    assert await store.get("credential") == value
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_delete_sidecar_failure_keeps_long_value_active(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = KeyringSecretStore(SERVICE)
    value = "A" * 1900
    await store.set("credential", value)
    original_entries = limited_keyring.entries.copy()

    def fail_sidecar_delete(service: str, key: str) -> None:
        if service == SERVICE and key == _manifest_key("credential"):
            raise RuntimeError("private sidecar delete failure")
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_sidecar_delete)

    with pytest.raises(SecretStorageError):
        await store.delete("credential")

    assert await store.get("credential") == value
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_delete_persistent_chunk_failure_deactivates_manifest(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)
    bottom_message = "private persistent chunk delete failure"

    def fail_chunk_delete(service: str, key: str) -> None:
        if service == SERVICE and ":chunk:" in key:
            raise RuntimeError(bottom_message)
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_chunk_delete)

    with pytest.raises(
        SecretStorageError,
        match="^credential storage cleanup failed$",
    ) as caught:
        await store.delete("credential")

    assert bottom_message not in str(caught.value)
    assert (SERVICE, _manifest_key("credential")) not in limited_keyring.entries
    assert _chunk_keys(limited_keyring, "credential")
    assert await store.get("credential") is None


@pytest.mark.asyncio
async def test_delete_removes_corrupted_manifest_entry(
    limited_keyring: LimitedKeyring,
) -> None:
    limited_keyring.entries[(SERVICE, "credential")] = (
        MANIFEST_PREFIX + '{"sha256":"invalid","chunks":1}'
    )
    store = KeyringSecretStore(SERVICE)

    await store.delete("credential")
    await store.delete("credential")

    assert limited_keyring.entries == {}


@pytest.mark.asyncio
async def test_missing_chunk_fails_closed(limited_keyring: LimitedKeyring) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)
    del limited_keyring.entries[(SERVICE, min(_chunk_keys(limited_keyring, "credential")))]

    with pytest.raises(SecretStorageError, match="^credential storage is corrupted$"):
        await store.get("credential")


@pytest.mark.asyncio
async def test_tampered_chunk_digest_fails_closed(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)
    chunk_key = min(_chunk_keys(limited_keyring, "credential"))
    limited_keyring.entries[(SERVICE, chunk_key)] = "tampered"

    with pytest.raises(SecretStorageError, match="^credential storage is corrupted$"):
        await store.get("credential")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        {"sha256": "a" * 64},
        {"chunks": 2},
        {"sha256": "not-a-digest", "chunks": "two"},
    ],
)
async def test_incomplete_or_invalid_manifest_like_legacy_value_is_read_raw(
    limited_keyring: LimitedKeyring,
    manifest: dict[str, object],
) -> None:
    value = MANIFEST_PREFIX + json.dumps(manifest)
    limited_keyring.entries[(SERVICE, "credential")] = value
    store = KeyringSecretStore(SERVICE)

    assert await store.get("credential") == value


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", INVALID_TYPED_MANIFESTS)
async def test_invalid_typed_manifest_get_fails_closed(
    limited_keyring: LimitedKeyring,
    manifest: dict[str, object],
) -> None:
    value = MANIFEST_PREFIX + json.dumps(manifest)
    limited_keyring.entries[(SERVICE, _manifest_key("credential"))] = value
    store = KeyringSecretStore(SERVICE)

    with pytest.raises(SecretStorageError, match="^credential storage is corrupted$"):
        await store.get("credential")


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", INVALID_TYPED_MANIFESTS)
async def test_invalid_typed_manifest_delete_fails_closed_without_changes(
    limited_keyring: LimitedKeyring,
    manifest: dict[str, object],
) -> None:
    value = MANIFEST_PREFIX + json.dumps(manifest)
    limited_keyring.entries[(SERVICE, _manifest_key("credential"))] = value
    original_entries = limited_keyring.entries.copy()
    store = KeyringSecretStore(SERVICE)

    with pytest.raises(SecretStorageError, match="^credential storage is corrupted$"):
        await store.delete("credential")

    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_partial_new_chunk_failure_preserves_old_entries(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    old_value = "A" * 1900
    await store.set("credential", old_value)
    original_entries = limited_keyring.entries.copy()
    limited_keyring.fail_on_set_call = limited_keyring.set_calls + 2

    with pytest.raises(SecretStorageError):
        await store.set("credential", "B" * 1900)

    assert await store.get("credential") == old_value
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_partial_write_retries_transient_rollback_delete_failure(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = KeyringSecretStore(SERVICE)
    old_value = "A" * 1900
    await store.set("credential", old_value)
    original_entries = limited_keyring.entries.copy()
    limited_keyring.fail_on_set_call = limited_keyring.set_calls + 2
    rollback_failure_pending = True

    def fail_first_new_chunk_delete(service: str, key: str) -> None:
        nonlocal rollback_failure_pending
        is_new_chunk = (service, key) not in original_entries and ":chunk:" in key
        if rollback_failure_pending and is_new_chunk:
            rollback_failure_pending = False
            raise RuntimeError("transient rollback delete failure")
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_first_new_chunk_delete)

    with pytest.raises(SecretStorageError):
        await store.set("credential", "B" * 1900)

    assert await store.get("credential") == old_value
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_persistent_rollback_failure_is_reported_distinctly(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = KeyringSecretStore(SERVICE)
    await store.set("credential", "A" * 1900)
    original_entries = limited_keyring.entries.copy()
    limited_keyring.fail_on_set_call = limited_keyring.set_calls + 2

    def fail_new_chunk_delete(service: str, key: str) -> None:
        if (service, key) not in original_entries and ":chunk:" in key:
            raise RuntimeError("private rollback backend detail")
        limited_keyring.delete_password(service, key)

    monkeypatch.setattr(keyring, "delete_password", fail_new_chunk_delete)

    with pytest.raises(
        SecretStorageError,
        match="^credential storage rollback failed$",
    ) as caught:
        await store.set("credential", "B" * 1900)

    assert "private rollback backend detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_same_digest_write_failure_does_not_remove_readable_chunks(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    value = "A" * 1900
    await store.set("credential", value)
    original_entries = limited_keyring.entries.copy()
    limited_keyring.fail_on_set_call = limited_keyring.set_calls + 2

    with pytest.raises(SecretStorageError):
        await store.set("credential", value)

    assert await store.get("credential") == value
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_replacing_uppercase_digest_manifest_cleans_its_distinct_chunk_keys(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    value = "A" * 1900
    await store.set("credential", value)
    manifest = _manifest(limited_keyring, "credential")
    lower_digest = str(manifest["sha256"])
    upper_digest = lower_digest.upper()
    chunk_count = manifest["chunks"]
    assert isinstance(chunk_count, int)
    assert not isinstance(chunk_count, bool)
    for index in range(chunk_count):
        chunk = limited_keyring.entries.pop(
            (SERVICE, f"credential:chunk:{lower_digest}:{index}")
        )
        limited_keyring.entries[
            (SERVICE, f"credential:chunk:{upper_digest}:{index}")
        ] = chunk
    manifest["sha256"] = upper_digest
    limited_keyring.entries[(SERVICE, _manifest_key("credential"))] = (
        MANIFEST_PREFIX + json.dumps(manifest)
    )

    await store.set("credential", value)

    assert await store.get("credential") == value
    assert all(upper_digest not in key for key in _chunk_keys(limited_keyring, "credential"))


@pytest.mark.asyncio
async def test_manifest_write_failure_preserves_old_entries(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    old_value = "A" * 1900
    await store.set("credential", old_value)
    original_entries = limited_keyring.entries.copy()
    limited_keyring.fail_on_set_call = limited_keyring.set_calls + 4

    with pytest.raises(SecretStorageError):
        await store.set("credential", "B" * 1900)

    assert await store.get("credential") == old_value
    assert limited_keyring.entries == original_entries


@pytest.mark.asyncio
async def test_value_requiring_more_than_maximum_chunks_is_rejected_before_writes(
    limited_keyring: LimitedKeyring,
) -> None:
    store = KeyringSecretStore(SERVICE)
    oversized_value = "A" * (900 * 10_000 + 1)

    with pytest.raises(SecretStorageError):
        await store.set("credential", oversized_value)

    assert limited_keyring.entries == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set", "delete"])
async def test_keyring_errors_are_wrapped_without_sensitive_details(
    limited_keyring: LimitedKeyring,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    key = "sensitive-key-name"
    value = "-----BEGIN PRIVATE KEY----- secret material"
    bottom_message = f"backend rejected {key}: {value}"

    def fail(*_args: object) -> None:
        raise RuntimeError(bottom_message)

    monkeypatch.setattr(keyring, f"{operation}_password", fail)
    store = KeyringSecretStore(SERVICE)

    with pytest.raises(SecretStorageError) as caught:
        if operation == "get":
            await store.get(key)
        elif operation == "set":
            await store.set(key, value)
        else:
            await store.delete(key)

    error_text = str(caught.value)
    assert key not in error_text
    assert value not in error_text
    assert bottom_message not in error_text


def _entry_keys(limited_keyring: LimitedKeyring) -> set[str]:
    return {key for service, key in limited_keyring.entries if service == SERVICE}


def _chunk_keys(limited_keyring: LimitedKeyring, key: str) -> set[str]:
    return {entry for entry in _entry_keys(limited_keyring) if entry.startswith(f"{key}:chunk:")}


def _manifest(limited_keyring: LimitedKeyring, key: str) -> dict[str, object]:
    value = limited_keyring.entries[(SERVICE, _manifest_key(key))]
    return json.loads(value.removeprefix(MANIFEST_PREFIX))


def _manifest_key(key: str) -> str:
    return f"{key}:manifest:v1"
