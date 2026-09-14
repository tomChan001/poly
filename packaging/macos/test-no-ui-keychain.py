"""Native CI acceptance using only an explicitly opened disposable keychain.

Never invoke on a user's machine. No real credential or keychain password is
accepted, and output contains assertion statuses only.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.core.macos_secrets import MacOSNoUISecretStore, NativeMacOSKeychain
from backend.app.core.secrets import SecretStorageError

LEGACY_SERVICE = 'com.poly.desktop.integrations'
KEY = 'oddpool:api_token'
VALUE = 'synthetic-no-ui-ci-value'
LONG_VALUE = 'synthetic-long-pem-fixture-' * 110
TEST_PASSWORD = b'synthetic-disposable-ci-keychain-password'
P = ctypes.c_void_p
U = ctypes.c_uint32
B = ctypes.c_bool
I = ctypes.c_int32


def check(status: int) -> None:
    if status:
        raise RuntimeError('native test operation failed')


def libraries():
    sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    declarations = {
        'SecKeychainSetUserInteractionAllowed': [B],
        'SecKeychainGetUserInteractionAllowed': [ctypes.POINTER(B)],
        'SecKeychainCreate': [ctypes.c_char_p, U, P, B, P, ctypes.POINTER(P)],
        'SecKeychainOpen': [ctypes.c_char_p, ctypes.POINTER(P)],
        'SecKeychainDelete': [P],
        'SecKeychainLock': [P],
        'SecKeychainUnlock': [P, U, P, B],
        'SecKeychainCopySearchList': [ctypes.POINTER(P)],
        'SecKeychainSetSearchList': [P],
    }
    for name, args in declarations.items():
        func = getattr(sec, name)
        func.argtypes = args
        func.restype = I
    core.CFRelease.argtypes = [P]
    core.CFRelease.restype = None
    check(sec.SecKeychainSetUserInteractionAllowed(False))
    return sec, core


def safe_keychain(path: Path) -> Path:
    runner_temp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    path = path.resolve()
    if not path.is_relative_to(runner_temp):
        raise RuntimeError('test target outside runner temp')
    if not path.parent.name.startswith('poly-no-ui-keychain-') or path.name != 'disposable.keychain-db':
        raise RuntimeError('not a disposable test target')
    return path


async def child(action: str, path: Path) -> None:
    sec, core = libraries()
    handle = P()
    check(sec.SecKeychainOpen(os.fsencode(safe_keychain(path)), ctypes.byref(handle)))
    try:
        backend = NativeMacOSKeychain(keychain=handle)
        executable = path.parent / 'runtime-identity.bin'
        store = MacOSNoUISecretStore(executable=executable, backend=backend)
        if action == 'save':
            backend.set_password(LEGACY_SERVICE, KEY, 'synthetic-legacy-sentinel')
            assert await store.get(KEY) is None
            await store.set(KEY, VALUE)
            await store.set('kalshi:private_key', LONG_VALUE)
            assert await store.get(KEY) == VALUE
            assert await store.get('kalshi:private_key') == LONG_VALUE
        elif action == 'restart':
            assert await store.get(KEY) == VALUE
            assert await store.get('kalshi:private_key') == LONG_VALUE
            await store.set(KEY, VALUE + '-updated')
            assert await store.get(KEY) == VALUE + '-updated'
        elif action == 'locked':
            check(sec.SecKeychainLock(handle))
            for operation in (store.get(KEY), store.set(KEY, 'synthetic-not-saved')):
                try:
                    await operation
                except SecretStorageError:
                    pass
                else:
                    raise AssertionError('locked keychain operation unexpectedly succeeded')
            check(sec.SecKeychainUnlock(handle, len(TEST_PASSWORD), TEST_PASSWORD, True))
            assert await store.get(KEY) == VALUE + '-updated'
        elif action == 'changed-build':
            assert await store.get(KEY) is None
            assert await store.get('kalshi:private_key') is None
        elif action == 'delete':
            assert backend.get_password(LEGACY_SERVICE, KEY) == 'synthetic-legacy-sentinel'
            assert await store.get(KEY) == VALUE + '-updated'
            await store.delete(KEY)
            await store.delete('kalshi:private_key')
            await store.delete(KEY)
            assert await store.get(KEY) is None
            assert await store.get('kalshi:private_key') is None
            assert backend.get_password(LEGACY_SERVICE, KEY) == 'synthetic-legacy-sentinel'
        else:
            raise ValueError('unknown test phase')
        interaction_allowed = B(True)
        check(sec.SecKeychainGetUserInteractionAllowed(ctypes.byref(interaction_allowed)))
        assert interaction_allowed.value is False
    finally:
        core.CFRelease(handle)


def acceptance() -> None:
    sec, core = libraries()
    parent = Path(tempfile.mkdtemp(prefix='poly-no-ui-keychain-', dir=os.environ['RUNNER_TEMP']))
    path = safe_keychain(parent / 'disposable.keychain-db')
    executable = parent / 'runtime-identity.bin'
    executable.write_bytes(b'synthetic-runtime-identity-v1')
    keychain = P()
    previous_search_list = P()
    check(sec.SecKeychainCopySearchList(ctypes.byref(previous_search_list)))
    created = False
    try:
        check(sec.SecKeychainCreate(
            os.fsencode(path), len(TEST_PASSWORD), TEST_PASSWORD, False, None, ctypes.byref(keychain)
        ))
        created = True
        # Creation may register this disposable file. Immediately restore the
        # prior list; every test operation passes the opened keychain explicitly.
        check(sec.SecKeychainSetSearchList(previous_search_list))
        check(sec.SecKeychainUnlock(keychain, len(TEST_PASSWORD), TEST_PASSWORD, True))
        for action in ('save', 'restart', 'locked', 'changed-build', 'delete'):
            if action == 'changed-build':
                executable.write_bytes(b'synthetic-runtime-identity-v2')
            elif action == 'delete':
                executable.write_bytes(b'synthetic-runtime-identity-v1')
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), '--child', action, str(path)],
                capture_output=True, timeout=15, check=False,
            )
            if result.returncode:
                # Child output is deliberately not forwarded to build logs.
                print(json.dumps({'no_ui_phase': action, 'passed': False,
                                  'exit_code': result.returncode}), flush=True)
                raise RuntimeError(f'no-ui acceptance phase failed: {action}')
            print(json.dumps({'no_ui_phase': action, 'passed': True}), flush=True)
        print(json.dumps({
            'native_no_ui_keychain': 'passed',
            'restart_persistence': True,
            'changed_build_isolated': True,
            'locked_read_rejected': True,
            'locked_write_preserved_value': True,
            'legacy_preserved': True,
        }), flush=True)
    finally:
        try:
            if created:
                check(sec.SecKeychainDelete(keychain))
        finally:
            check(sec.SecKeychainSetSearchList(previous_search_list))
            if keychain.value:
                core.CFRelease(keychain)
            core.CFRelease(previous_search_list)


if __name__ == '__main__':
    if sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('This acceptance runs only on a disposable GitHub macOS runner.')
    if not os.environ.get('RUNNER_TEMP'):
        raise SystemExit('RUNNER_TEMP is required.')
    try:
        if len(sys.argv) == 4 and sys.argv[1] == '--child':
            asyncio.run(child(sys.argv[2], Path(sys.argv[3])))
        elif len(sys.argv) == 1:
            acceptance()
        else:
            raise ValueError('invalid test arguments')
    except Exception as error:  # noqa: BLE001 - never emit credential-bearing exceptions
        # Exception type alone is safe and still distinguishes timeout/assertion.
        print(f'no-ui native acceptance failed ({type(error).__name__})', file=sys.stderr)
        raise SystemExit(1) from None
