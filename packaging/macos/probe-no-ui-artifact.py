"""Hosted-CI-only differential probe; synthetic values and status-only output."""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def command(args, *, env=None, data=None):
    return subprocess.run(args, env=env, input=data, capture_output=True, timeout=30)


def default_status():
    sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    sec.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_bool]
    sec.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
    sec.SecKeychainSetUserInteractionAllowed(False)
    sec.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    sec.SecKeychainCopyDefault.restype = ctypes.c_int32
    handle = ctypes.c_void_p()
    status = sec.SecKeychainCopyDefault(ctypes.byref(handle))
    if handle:
        core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease.restype = None
        core.CFRelease(handle)
    print(json.dumps({'native_default_status': status}))


def probe(dmg):
    runner = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix='poly-no-ui-probe-', dir=runner)).resolve()
    assert root.parent == runner
    mount = root / 'mount'
    mount.mkdir()
    app = root / 'Poly.app'
    keychain = root / 'disposable.keychain-db'
    isolated_home = root / 'home'
    isolated_home.mkdir()
    security = '/usr/bin/security'
    original_env = dict(os.environ)
    isolated_env = dict(os.environ, HOME=str(isolated_home))
    saved_default = command([security, 'default-keychain', '-d', 'user']).stdout.decode().strip().strip('"')
    saved_list = [line.strip().strip('"') for line in command([security, 'list-keychains', '-d', 'user']).stdout.decode().splitlines()]
    assert saved_default and saved_list
    created = mounted = False
    try:
        command(['/usr/bin/hdiutil', 'attach', dmg, '-mountpoint', str(mount), '-nobrowse', '-readonly']).check_returncode()
        mounted = True
        command(['/usr/bin/ditto', str(mount / 'Poly.app'), str(app)]).check_returncode()
        command([security, 'create-keychain', '-p', 'synthetic-ci-only', str(keychain)]).check_returncode()
        created = True
        command([security, 'unlock-keychain', '-p', 'synthetic-ci-only', str(keychain)]).check_returncode()
        command([security, 'list-keychains', '-d', 'user', '-s', str(keychain)]).check_returncode()
        command([security, 'default-keychain', '-d', 'user', '-s', str(keychain)]).check_returncode()
        executable = app / 'Contents/Resources/poly-runtime/poly-runtime'
        for label, env in [('original-home', original_env), ('isolated-home', isolated_env)]:
            result = command([sys.executable, str(Path(__file__).resolve()), '--default-status'], env=env)
            print(label, result.stdout.decode().strip(), flush=True)
            for action in ['set', 'verify', 'delete']:
                result = command([str(executable), '--keychain-smoke', action, '--account', 'ci-smoke-probe-' + label], env=env, data=b'synthetic-probe-value')
                print(json.dumps({'context': label, 'action': action, 'exit': result.returncode}), flush=True)
    finally:
        if created:
            restored_default = command([security, 'default-keychain', '-d', 'user', '-s', saved_default], env=original_env)
            restored_list = command([security, 'list-keychains', '-d', 'user', '-s', *saved_list], env=original_env)
            restored_default.check_returncode()
            restored_list.check_returncode()
            command([security, 'delete-keychain', str(keychain)], env=original_env).check_returncode()
        if mounted:
            command(['/usr/bin/hdiutil', 'detach', str(mount)]).check_returncode()


if __name__ == '__main__':
    if sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('requires a disposable hosted macOS runner')
    if sys.argv[1:] == ['--default-status']:
        default_status()
    else:
        probe(sys.argv[1])
