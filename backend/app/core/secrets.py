import asyncio
from typing import Protocol


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
        import keyring

        return await asyncio.to_thread(keyring.get_password, self._service_name, key)

    async def set(self, key: str, value: str) -> None:
        import keyring

        await asyncio.to_thread(keyring.set_password, self._service_name, key, value)

    async def delete(self, key: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            await asyncio.to_thread(keyring.delete_password, self._service_name, key)
        except PasswordDeleteError:
            # Delete is idempotent so operators can safely retry after a timeout.
            return
