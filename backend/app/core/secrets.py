import asyncio
import hashlib
import json
import logging
import string
from typing import Protocol

_CHUNK_SIZE = 900
_MAX_CHUNKS = 10_000
_MANIFEST_PREFIX = "poly-keyring-chunks:v1:"
_MANIFEST_KIND = "chunked-secret"
_CORRUPTED_MESSAGE = "credential storage is corrupted"
_CLEANUP_WARNING_MESSAGE = "credential storage cleanup failed"
_OPERATION_ERROR_MESSAGE = "credential storage operation failed"
_ROLLBACK_ERROR_MESSAGE = "credential storage rollback failed"
_LOGGER = logging.getLogger(__name__)


class SecretStorageError(RuntimeError):
    """Raised when the operating-system credential store cannot be used safely."""


class SecretStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...


class InMemorySecretStore:
    """Test implementation with the same write/read boundary as the OS store."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def set(self, key: str, value: str) -> None:
        self._values[key] = value

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)


class KeyringSecretStore:
    """Store credentials under the operating-system identity running the API."""

    def __init__(self, service_name: str) -> None:
        self._service_name = service_name

    async def get(self, key: str) -> str | None:
        value = await self._get_password(key)
        if value is None:
            return None
        manifest = _parse_manifest(value)
        if manifest is None:
            return value

        digest, chunk_count = manifest
        chunks: list[str] = []
        for index in range(chunk_count):
            chunk = await self._get_password(_chunk_key(key, digest, index))
            if chunk is None:
                raise SecretStorageError(_CORRUPTED_MESSAGE)
            chunks.append(chunk)

        result = "".join(chunks)
        if hashlib.sha256(result.encode()).hexdigest() != digest.lower():
            raise SecretStorageError(_CORRUPTED_MESSAGE)
        return result

    async def set(self, key: str, value: str) -> None:
        old_value = await self._get_password(key)
        old_manifest = _parse_manifest(old_value) if old_value is not None else None
        if len(value) <= _CHUNK_SIZE:
            await self._set_password(key, value)
            if old_manifest is not None:
                await self._cleanup_manifest_chunks(key, old_manifest)
            return

        chunk_count = (len(value) + _CHUNK_SIZE - 1) // _CHUNK_SIZE
        if chunk_count > _MAX_CHUNKS:
            raise SecretStorageError(_OPERATION_ERROR_MESSAGE)
        digest = hashlib.sha256(value.encode()).hexdigest()
        chunks = [
            value[offset : offset + _CHUNK_SIZE]
            for offset in range(0, len(value), _CHUNK_SIZE)
        ]
        written_chunk_keys: list[str] = []
        try:
            for index, chunk in enumerate(chunks):
                chunk_key = _chunk_key(key, digest, index)
                await self._set_password(chunk_key, chunk)
                written_chunk_keys.append(chunk_key)
            manifest = _MANIFEST_PREFIX + json.dumps(
                {
                    "kind": _MANIFEST_KIND,
                    "sha256": digest,
                    "chunks": len(chunks),
                },
                separators=(",", ":"),
            )
            await self._set_password(key, manifest)
        except SecretStorageError:
            if old_manifest is None or old_manifest[0] != digest:
                await self._rollback_keys(written_chunk_keys)
            raise

        if old_manifest is not None and old_manifest[0] != digest:
            await self._cleanup_manifest_chunks(key, old_manifest)

    async def delete(self, key: str) -> None:
        value = await self._get_password(key)
        manifest = _parse_manifest(value) if value is not None else None
        if manifest is not None:
            digest, chunk_count = manifest
            await self._delete_keys(
                [_chunk_key(key, digest, index) for index in range(chunk_count)]
            )
        await self._delete_keys([key])

    async def _get_password(self, key: str) -> str | None:
        import keyring

        try:
            return await asyncio.to_thread(
                keyring.get_password,
                self._service_name,
                key,
            )
        except Exception:  # noqa: BLE001 - backends raise platform-specific errors.
            raise SecretStorageError(_OPERATION_ERROR_MESSAGE) from None

    async def _set_password(self, key: str, value: str) -> None:
        import keyring

        try:
            await asyncio.to_thread(
                keyring.set_password,
                self._service_name,
                key,
                value,
            )
        except Exception:  # noqa: BLE001 - backends raise platform-specific errors.
            raise SecretStorageError(_OPERATION_ERROR_MESSAGE) from None

    async def _delete_password(self, key: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            await asyncio.to_thread(keyring.delete_password, self._service_name, key)
        except PasswordDeleteError:
            return
        except Exception:  # noqa: BLE001 - backends raise platform-specific errors.
            raise SecretStorageError(_OPERATION_ERROR_MESSAGE) from None

    async def _delete_manifest_chunks(
        self,
        key: str,
        manifest: tuple[str, int],
    ) -> None:
        digest, chunk_count = manifest
        await self._delete_keys(
            [_chunk_key(key, digest, index) for index in range(chunk_count)]
        )

    async def _cleanup_manifest_chunks(
        self,
        key: str,
        manifest: tuple[str, int],
    ) -> None:
        for _attempt in range(2):
            try:
                await self._delete_manifest_chunks(key, manifest)
            except SecretStorageError:
                continue
            return
        _LOGGER.warning(_CLEANUP_WARNING_MESSAGE)

    async def _delete_keys(
        self,
        keys: list[str],
    ) -> None:
        first_error: SecretStorageError | None = None
        for key in keys:
            try:
                await self._delete_password(key)
            except SecretStorageError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    async def _rollback_keys(self, keys: list[str]) -> None:
        for _attempt in range(2):
            try:
                await self._delete_keys(keys)
            except SecretStorageError:
                continue
            return
        raise SecretStorageError(_ROLLBACK_ERROR_MESSAGE) from None


def _chunk_key(key: str, digest: str, index: int) -> str:
    return f"{key}:chunk:{digest}:{index}"


def _parse_manifest(value: str) -> tuple[str, int] | None:
    if not value.startswith(_MANIFEST_PREFIX):
        return None
    try:
        parsed = json.loads(value.removeprefix(_MANIFEST_PREFIX))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("kind") != _MANIFEST_KIND:
        return None
    digest = parsed.get("sha256")
    chunks = parsed.get("chunks")
    if not isinstance(digest, str):
        raise SecretStorageError(_CORRUPTED_MESSAGE)
    if len(digest) != 64 or not all(
        character in string.hexdigits for character in digest
    ):
        raise SecretStorageError(_CORRUPTED_MESSAGE)
    if not isinstance(chunks, int) or isinstance(chunks, bool):
        raise SecretStorageError(_CORRUPTED_MESSAGE)
    if not 1 <= chunks <= _MAX_CHUNKS:
        raise SecretStorageError(_CORRUPTED_MESSAGE)
    return digest, chunks
