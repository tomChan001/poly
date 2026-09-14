"""Desktop-only native Keychain storage that never permits interaction."""

import asyncio
import ctypes
import hashlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from backend.app.core.secrets import KeyringSecretStore, SecretStorageError

_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
_ERROR = "credential storage operation failed"
_NOT_FOUND = -25300


class NativeSecretBackend(Protocol):
    def get_password(self, service: str, key: str) -> str | None: ...

    def set_password(self, service: str, key: str, value: str) -> None: ...

    def delete_password(self, service: str, key: str) -> None: ...


def service_for_executable(
    executable: str | Path,
    service_name: str = "com.poly.desktop.integrations",
) -> str:
    try:
        with Path(executable).open("rb") as binary:
            digest = hashlib.file_digest(binary, "sha256").hexdigest()
        return f"{service_name}.no-ui.v1.{digest}"
    except Exception:  # noqa: BLE001 - sanitize filesystem and hash failures.
        raise SecretStorageError(_ERROR) from None


def disable_keychain_ui() -> None:
    """Never re-enable the process-wide Security framework interaction flag."""
    try:
        security = ctypes.CDLL(_SECURITY_FRAMEWORK)
        disable = security.SecKeychainSetUserInteractionAllowed
        disable.argtypes = [ctypes.c_bool]
        disable.restype = ctypes.c_int32
        if disable(False) != 0:
            raise SecretStorageError(_ERROR)
    except Exception:  # noqa: BLE001 - no native diagnostics cross this boundary.
        raise SecretStorageError(_ERROR) from None


class NativeMacOSKeychain:
    """File-Keychain API with guarded calls and non-destructive updates.

    A supplied keychain handle is borrowed, never closed here. Production uses
    the default keychain; an explicit handle allows isolated native testing.
    No keyring environment variables or automatic backend plugins are read.
    """

    def __init__(
        self,
        *,
        keychain: ctypes.c_void_p | None = None,
        disable_ui: Callable[[], None] | None = None,
    ) -> None:
        self._keychain = keychain
        self._disable_ui = disable_ui or disable_keychain_ui
        try:
            self._security = ctypes.CDLL(_SECURITY_FRAMEWORK)
            self._core = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            pointer = ctypes.c_void_p
            size = ctypes.c_uint32
            text = ctypes.c_char_p
            self._security.SecKeychainFindGenericPassword.argtypes = [
                pointer,
                size,
                text,
                size,
                text,
                ctypes.POINTER(size),
                ctypes.POINTER(pointer),
                ctypes.POINTER(pointer),
            ]
            self._security.SecKeychainAddGenericPassword.argtypes = [
                pointer,
                size,
                text,
                size,
                text,
                size,
                text,
                ctypes.POINTER(pointer),
            ]
            self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
                pointer,
                pointer,
                size,
                text,
            ]
            self._security.SecKeychainItemDelete.argtypes = [pointer]
            self._security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
            for name in (
                "SecKeychainFindGenericPassword",
                "SecKeychainAddGenericPassword",
                "SecKeychainItemModifyAttributesAndData",
                "SecKeychainItemDelete",
                "SecKeychainItemFreeContent",
            ):
                getattr(self._security, name).restype = ctypes.c_int32
            self._core.CFRelease.argtypes = [pointer]
            self._core.CFRelease.restype = None
        except Exception:  # noqa: BLE001 - sanitize native library loading errors.
            raise SecretStorageError(_ERROR) from None

    def _call(self, name: str, *args) -> int:
        try:
            self._disable_ui()
            return getattr(self._security, name)(*args)
        except Exception:  # noqa: BLE001 - failed suppression must fail closed.
            raise SecretStorageError(_ERROR) from None

    @staticmethod
    def _check(status: int) -> None:
        if status != 0:
            raise SecretStorageError(_ERROR)

    def _find(
        self, service: str, key: str, *, length=None, data=None, item=None
    ) -> int:
        service_bytes, key_bytes = service.encode("utf-8"), key.encode("utf-8")
        status = self._call(
            "SecKeychainFindGenericPassword",
            self._keychain,
            len(service_bytes),
            service_bytes,
            len(key_bytes),
            key_bytes,
            length,
            data,
            item,
        )
        if status != _NOT_FOUND:
            self._check(status)
        return status

    def get_password(self, service: str, key: str) -> str | None:
        length, data = ctypes.c_uint32(), ctypes.c_void_p()
        status = self._find(
            service, key, length=ctypes.byref(length), data=ctypes.byref(data)
        )
        if status == _NOT_FOUND:
            return None
        try:
            return ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            self._check(self._security.SecKeychainItemFreeContent(None, data))

    def set_password(self, service: str, key: str, value: str) -> None:
        item = ctypes.c_void_p()
        status = self._find(service, key, item=ctypes.byref(item))
        value_bytes = value.encode("utf-8")
        if status == _NOT_FOUND:
            service_bytes, key_bytes = service.encode("utf-8"), key.encode("utf-8")
            self._check(
                self._call(
                    "SecKeychainAddGenericPassword",
                    self._keychain,
                    len(service_bytes),
                    service_bytes,
                    len(key_bytes),
                    key_bytes,
                    len(value_bytes),
                    value_bytes,
                    None,
                )
            )
            return
        try:
            self._check(
                self._call(
                    "SecKeychainItemModifyAttributesAndData",
                    item,
                    None,
                    len(value_bytes),
                    value_bytes,
                )
            )
        finally:
            self._core.CFRelease(item)

    def delete_password(self, service: str, key: str) -> None:
        item = ctypes.c_void_p()
        if self._find(service, key, item=ctypes.byref(item)) == _NOT_FOUND:
            return
        try:
            status = self._call("SecKeychainItemDelete", item)
            if status != _NOT_FOUND:
                self._check(status)
        finally:
            self._core.CFRelease(item)


class MacOSNoUISecretStore(KeyringSecretStore):
    def __init__(
        self,
        service_name: str = "com.poly.desktop.integrations",
        *,
        executable: str | Path | None = None,
        backend: NativeSecretBackend | None = None,
        disable_ui: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            service_for_executable(executable or sys.executable, service_name)
        )
        self._disable_ui = disable_ui or disable_keychain_ui
        self._native_backend = backend

    def _invoke(self, name: str, *args: str):
        try:
            self._disable_ui()
            if self._native_backend is None:
                self._native_backend = NativeMacOSKeychain(disable_ui=self._disable_ui)
            operation = getattr(self._native_backend, name)
            return operation(self._service_name, *args)
        except Exception:  # noqa: BLE001 - sanitize all backend-specific errors.
            raise SecretStorageError(_ERROR) from None

    async def _get_password(self, key: str) -> str | None:
        return await asyncio.to_thread(self._invoke, "get_password", key)

    async def _set_password(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._invoke, "set_password", key, value)

    async def _delete_password(self, key: str) -> None:
        # Native backend distinguishes item-not-found from denied/locked errors.
        await asyncio.to_thread(self._invoke, "delete_password", key)
