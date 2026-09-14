from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_ci_verifies_native_no_ui_storage_before_packaging() -> None:
    workflow = (ROOT / '.github/workflows/macos-desktop.yml').read_text(encoding='utf-8')
    assert 'Run disposable no-UI keychain acceptance' in workflow
    assert 'uv run --frozen python packaging/macos/test-no-ui-keychain.py' in workflow
    assert workflow.index('Run disposable no-UI keychain acceptance') < workflow.index('Build frontend')
    assert 'packaging/macos/tests/test_no_ui_keychain_contract.py' in workflow


def test_native_acceptance_never_targets_login_keychain() -> None:
    script = (ROOT / 'packaging/macos/test-no-ui-keychain.py').read_text(encoding='utf-8')
    assert 'GITHUB_ACTIONS' in script and 'RUNNER_TEMP' in script
    assert 'NativeMacOSKeychain(keychain=' in script
    assert 'SecKeychainSetDefault' not in script
    assert 'timeout=15' in script
    assert 'legacy_preserved' in script
    assert 'locked_read_rejected' in script
