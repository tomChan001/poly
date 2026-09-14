import ctypes
import hashlib
import importlib
from types import SimpleNamespace

import keyring
import pytest

from backend.app.core.secrets import SecretStorageError


def subject():
    try:
        return importlib.import_module("backend.app.core.macos_secrets")
    except ModuleNotFoundError:
        pytest.fail("macOS no-UI credential store is not implemented")


class SyntheticBackend:
    def __init__(self):
        self.entries = {}
        self.calls = []
        self.failure = None

    def disable(self):
        self.calls.append("disable")

    def operation(self, name, service, key):
        assert self.calls[-1] == "disable"
        self.calls.append((name, service, key))
        if self.failure:
            raise self.failure

    def get_password(self, service, key):
        self.operation("get", service, key)
        return self.entries.get((service, key))

    def set_password(self, service, key, value):
        self.operation("set", service, key)
        self.entries[(service, key)] = value

    def delete_password(self, service, key):
        self.operation("delete", service, key)
        self.entries.pop((service, key), None)


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "poly-runtime"
    path.write_bytes(b"synthetic binary one")
    return path


def test_namespace_uses_binary_content_not_path(executable, tmp_path):
    module = subject()
    moved = tmp_path / "moved-runtime"
    moved.write_bytes(executable.read_bytes())
    expected = (
        "com.poly.desktop.integrations.no-ui.v1."
        + hashlib.sha256(executable.read_bytes()).hexdigest()
    )
    assert module.service_for_executable(executable) == expected
    assert module.service_for_executable(moved) == expected
    executable.write_bytes(b"updated synthetic binary")
    assert module.service_for_executable(executable) != expected


def test_unreadable_binary_fails_closed_with_sanitized_error(tmp_path):
    with pytest.raises(
        SecretStorageError, match="^credential storage operation failed$"
    ):
        subject().service_for_executable(tmp_path / "private-sensitive-path")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["synthetic secret", "synthetic long value" * 150],
    ids=["short", "chunked"],
)
async def test_restart_retains_values_update_is_empty_and_legacy_is_untouched(
    executable, value
):
    module = subject()
    backend = SyntheticBackend()
    legacy = ("com.poly.desktop.integrations", "api-key")
    backend.entries[legacy] = "legacy synthetic value"

    def store():
        return module.MacOSNoUISecretStore(
            executable=executable, backend=backend, disable_ui=backend.disable
        )

    original = store()
    assert await original.get("api-key") is None
    await original.set("api-key", value)
    assert await store().get("api-key") == value
    executable.write_bytes(b"different synthetic binary")
    updated = store()
    assert await updated.get("api-key") is None
    await updated.delete("api-key")
    assert await original.get("api-key") == value
    await original.delete("api-key")
    assert await original.get("api-key") is None
    assert backend.entries == {legacy: "legacy synthetic value"}
    assert all(
        call[1] != legacy[0] for call in backend.calls if isinstance(call, tuple)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set", "delete"])
async def test_ui_disable_failure_prevents_all_native_access(executable, operation):
    backend = SyntheticBackend()

    def fail():
        raise OSError("sensitive native error")

    store = subject().MacOSNoUISecretStore(
        executable=executable, backend=backend, disable_ui=fail
    )
    arguments = ("api-key", "synthetic") if operation == "set" else ("api-key",)
    with pytest.raises(
        SecretStorageError, match="^credential storage operation failed$"
    ):
        await getattr(store, operation)(*arguments)
    assert backend.calls == []
    assert backend.entries == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set", "delete"])
async def test_native_error_is_sanitized_and_never_reports_success(
    executable, operation
):
    backend = SyntheticBackend()
    backend.failure = OSError("synthetic secret in native exception")
    store = subject().MacOSNoUISecretStore(
        executable=executable, backend=backend, disable_ui=backend.disable
    )
    arguments = ("api-key", "synthetic") if operation == "set" else ("api-key",)
    with pytest.raises(
        SecretStorageError, match="^credential storage operation failed$"
    ):
        await getattr(store, operation)(*arguments)
    assert backend.entries == {}


@pytest.mark.parametrize("status", [0, -25308])
def test_native_ui_disabler_uses_fixed_framework_and_boolean_false(monkeypatch, status):
    module = subject()
    calls = []

    def disable(allowed):
        calls.append(allowed)
        return status

    def library(path):
        assert path == "/System/Library/Frameworks/Security.framework/Security"
        return SimpleNamespace(SecKeychainSetUserInteractionAllowed=disable)

    monkeypatch.setattr(ctypes, "CDLL", library)
    if status:
        with pytest.raises(SecretStorageError):
            module.disable_keychain_ui()
    else:
        module.disable_keychain_ui()
    assert calls == [False]
    assert disable.argtypes == [ctypes.c_bool]
    assert disable.restype is ctypes.c_int32


@pytest.mark.asyncio
async def test_global_keyring_selection_is_never_used(monkeypatch, executable):
    def reject(*args, **kwargs):
        pytest.fail("automatic backend selection was used")

    monkeypatch.setattr(keyring, "get_keyring", reject)
    monkeypatch.setattr(keyring, "get_password", reject)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    backend = SyntheticBackend()
    store = subject().MacOSNoUISecretStore(
        executable=executable, backend=backend, disable_ui=backend.disable
    )
    assert await store.get("api-key") is None


def test_native_backend_is_available():
    assert hasattr(subject(), "NativeMacOSKeychain"), (
        "native backend is not implemented"
    )


class NativeFunction:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


class SyntheticSecurity:
    def __init__(self):
        self.value = None
        self.calls = []
        self.failure = None
        self.buffer = None
        self.SecKeychainFindGenericPassword = NativeFunction(self.find)
        self.SecKeychainAddGenericPassword = NativeFunction(self.add)
        self.SecKeychainItemModifyAttributesAndData = NativeFunction(self.modify)
        self.SecKeychainItemDelete = NativeFunction(self.delete)
        self.SecKeychainItemFreeContent = NativeFunction(
            lambda *args: self.calls.append("free") or 0
        )
        self.CFRelease = NativeFunction(lambda *args: self.calls.append("release"))

    def disable(self):
        self.calls.append("disable")

    def operation(self, name):
        assert self.calls[-1] == "disable"
        self.calls.append(name)
        return self.failure or 0

    def find(
        self,
        keychain,
        service_length,
        service,
        account_length,
        account,
        password_length,
        password_data,
        item,
    ):
        assert keychain.value == 42
        assert service_length == len(service) and account_length == len(account)
        status = self.operation("find")
        if status:
            return status
        if self.value is None:
            return -25300
        if password_data is not None:
            self.buffer = ctypes.create_string_buffer(self.value)
            ctypes.cast(password_length, ctypes.POINTER(ctypes.c_uint32))[0] = len(
                self.value
            )
            ctypes.cast(password_data, ctypes.POINTER(ctypes.c_void_p))[0] = (
                ctypes.addressof(self.buffer)
            )
        if item is not None:
            ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = 99
        return 0

    def add(
        self,
        keychain,
        service_length,
        service,
        account_length,
        account,
        value_length,
        value,
        item,
    ):
        assert keychain.value == 42
        status = self.operation("add")
        if not status:
            self.value = value[:value_length]
        return status

    def modify(self, item, attributes, value_length, value):
        assert item.value == 99
        status = self.operation("modify")
        if not status:
            self.value = value[:value_length]
        return status

    def delete(self, item):
        assert item.value == 99
        status = self.operation("delete")
        if not status:
            self.value = None
        return status


@pytest.fixture
def native_backend(monkeypatch):
    module = subject()
    assert hasattr(module, "NativeMacOSKeychain"), "native backend is not implemented"
    api = SyntheticSecurity()
    paths = []

    def library(path):
        paths.append(path)
        return api

    monkeypatch.setattr(ctypes, "CDLL", library)
    backend = module.NativeMacOSKeychain(
        keychain=ctypes.c_void_p(42), disable_ui=api.disable
    )
    assert paths == [
        "/System/Library/Frameworks/Security.framework/Security",
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
    ]
    return backend, api


def test_native_round_trip_update_delete_uses_explicit_handle(native_backend):
    backend, api = native_backend
    assert backend.get_password("service", "account") is None
    backend.delete_password("service", "account")
    backend.set_password("service", "account", "synthetic 初始")
    assert backend.get_password("service", "account") == "synthetic 初始"
    backend.set_password("service", "account", "replacement")
    assert backend.get_password("service", "account") == "replacement"
    assert api.calls.count("add") == 1
    assert api.calls.count("modify") == 1
    assert api.calls.count("delete") == 0
    assert api.calls.count("free") == 2
    assert api.calls.count("release") == 1
    backend.delete_password("service", "account")
    assert api.calls.count("release") == 2
    assert backend.get_password("service", "account") is None


@pytest.mark.parametrize(
    "operation", ["get_password", "set_password", "delete_password"]
)
def test_native_locked_lookup_propagates_sanitized_error(native_backend, operation):
    backend, api = native_backend
    api.failure = -25308
    arguments = (
        ("service", "account", "synthetic")
        if operation == "set_password"
        else ("service", "account")
    )
    with pytest.raises(
        SecretStorageError, match="^credential storage operation failed$"
    ):
        getattr(backend, operation)(*arguments)
    assert api.calls == ["disable", "find"]


def test_native_failed_update_preserves_existing_value_and_releases_handle(
    native_backend,
):
    backend, api = native_backend
    api.value = b"original synthetic"

    def reject(*args):
        api.operation("modify")
        return -25308

    api.SecKeychainItemModifyAttributesAndData.implementation = reject
    with pytest.raises(SecretStorageError):
        backend.set_password("service", "account", "replacement")
    assert api.value == b"original synthetic"
    assert api.calls == ["disable", "find", "disable", "modify", "release"]


def test_native_disable_failure_stops_before_lookup(native_backend):
    backend, api = native_backend

    def reject():
        raise RuntimeError("synthetic sensitive diagnostic")

    backend._disable_ui = reject
    with pytest.raises(
        SecretStorageError, match="^credential storage operation failed$"
    ):
        backend.get_password("service", "account")
    assert api.calls == []
