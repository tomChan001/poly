import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "macos-desktop.yml"
RUNBOOK = ROOT / "docs" / "runbooks" / "macos-desktop-build.md"
README = ROOT / "README.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_workflow_is_read_only_and_uses_the_exact_native_matrix() -> None:
    workflow = read(WORKFLOW)
    assert re.search(r"(?m)^permissions:\s*\n\s+contents: read\s*$", workflow)
    for runner, target, architecture, artifact in (
        ("macos-15", "aarch64-apple-darwin", "arm64", "Poly-macos-arm64"),
        (
            "macos-15-intel",
            "x86_64-apple-darwin",
            "x86_64",
            "Poly-macos-x86_64",
        ),
    ):
        entry = re.compile(
            rf"runner: {re.escape(runner)}.*?"
            rf"rust_target: {re.escape(target)}.*?"
            rf"py_arch: {re.escape(architecture)}.*?"
            rf"artifact: {re.escape(artifact)}",
            re.DOTALL,
        )
        assert entry.search(workflow)
    assert "universal" not in workflow.lower()
    assert "aws-actions/configure-aws-credentials" not in workflow
    assert not re.search(
        r"(?im)^\s*(?:run:\s*)?aws\s+(?:cloudformation|s3|deploy)", workflow
    )
    assert not re.search(r"(?im)\bdeploy(?:ment)?\b", workflow)


def test_workflow_separates_validation_from_protected_release_signing() -> None:
    workflow = read(WORKFLOW)
    for trigger in ("pull_request:", "workflow_dispatch:", "tags:"):
        assert trigger in workflow
    assert re.search(r"tags:\s*\n\s+- ['\"]v\*['\"]", workflow)
    assert "github.event_name == 'push'" in workflow
    assert "startsWith(github.ref, 'refs/tags/v')" in workflow
    for secret in (
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "KEYCHAIN_PASSWORD",
        "APPLE_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    ):
        assert f"secrets.{secret}" in workflow
    assert "security create-keychain" in workflow
    assert "security import" in workflow
    assert "security delete-keychain" in workflow
    assert re.search(r"if:\s*\$\{\{\s*always\(\)", workflow)
    assert "echo ${{ secrets." not in workflow
    assert 'echo "$APPLE_' not in workflow


def test_workflow_has_the_required_ordered_locked_pipeline() -> None:
    workflow = read(WORKFLOW)
    ordered_markers = (
        "actions/checkout@v",
        "actions/setup-node@v",
        "npm ci",
        "astral-sh/setup-uv@v",
        "uv sync --frozen",
        "uv run --frozen pytest backend/tests/unit backend/tests/security",
        "npm run lint",
        "npm test",
        "npm run build",
        "rustup target add",
        "cargo fmt --manifest-path src-tauri/Cargo.toml -- --check",
        "cargo clippy --locked --manifest-path src-tauri/Cargo.toml",
        "cargo test --locked --manifest-path src-tauri/Cargo.toml",
        "packaging/macos/fetch-postgres.sh",
        "packaging/macos/build-runtime.sh",
        "packaging/macos/verify-runtime.sh",
        "cargo tauri build --target",
        "xcrun stapler",
        "packaging/macos/verify-bundle.sh",
        "actions/upload-artifact@v",
    )
    positions = [workflow.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    for uses in re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s]+)", workflow):
        assert "@" in uses
        assert not uses.endswith(("@main", "@master"))


def test_smoke_uses_an_isolated_home_installed_dmg_and_command_guards() -> None:
    workflow = read(WORKFLOW)
    for marker in (
        "mktemp -d",
        "SMOKE_HOME",
        "SMOKE_APPLICATIONS",
        "hdiutil attach",
        "hdiutil detach",
        "ditto",
        "open -n",
        "KeyringSecretStore",
        "await store.set",
        "await store.get",
        "await store.delete",
        "security default-keychain -d user -s",
        "security list-keychains -d user -s",
        'tell application id "com.poly.desktop" to quit',
        'packaging/macos/verify-bundle.sh" --enumerate-runtime',
        "127.0.0.1:",
    ):
        assert marker in workflow
    assert 'export PATH="${GUARD_DIR}:/usr/bin:/bin:/usr/sbin:/sbin"' in workflow
    assert "$(seq " not in workflow
    assert "for ((attempt = 1; attempt <=" in workflow
    for absolute_tool in (
        "/usr/bin/hdiutil",
        "/usr/bin/ditto",
        "/usr/bin/open",
        "/usr/bin/security",
        "/usr/bin/osascript",
        "/usr/sbin/lsof",
    ):
        assert absolute_tool in workflow
    assert workflow.count("open -n") >= 2
    assert "security add-generic-password" not in workflow
    assert "security find-generic-password" not in workflow
    assert re.search(
        r"keyring_probe set.*?/usr/bin/open -n.*?tell application id.*?"
        r"/usr/bin/open -n.*?keyring_probe get.*?keyring_probe delete",
        workflow,
        re.DOTALL,
    )


def test_smoke_audits_app_descendant_executables_not_just_path_lookups() -> None:
    workflow = read(WORKFLOW)
    for marker in (
        "/bin/ps -ww -axo pid=,ppid=,comm=",
        '/usr/bin/pgrep -f "^${APP_EXECUTABLE}',
        "/usr/sbin/lsof -nP -a -p",
        "AUDIT_FAILURE",
        "audit_app_processes &",
        '"${INSTALLED_APP}"/*',
        "/System/Library/*",
        "/usr/libexec/*",
    ):
        assert marker in workflow
    for forbidden_command in (
        "python",
        "python3",
        "node",
        "postgres",
        "aws",
        "docker",
        "brew",
    ):
        assert f'"{forbidden_command}"' in workflow
    assert '/usr/bin/grep -Fq "${APP_EXECUTABLE}" "${AUDIT_LOG}"' in workflow
    assert '/usr/bin/grep -Fq "${RUNTIME_ROOT}/" "${AUDIT_LOG}"' in workflow
    assert '[[ ! -s "${AUDIT_FAILURE}" ]]' in workflow


def test_non_release_diagnostics_are_collected_and_uploaded_even_on_failure() -> None:
    workflow = read(WORKFLOW)
    assert "Collect sanitized validation diagnostics" in workflow
    assert "Upload validation diagnostics" in workflow
    assert re.search(
        r"Upload validation diagnostics.*?if:\s*\$\{\{\s*always\(\).*?"
        r"actions/upload-artifact@v4",
        workflow,
        re.DOTALL,
    )
    assert "poly-diagnostics" in workflow


def test_runbook_documents_release_install_recovery_and_platform_limits() -> None:
    runbook = read(RUNBOOK)
    for item in (
        "Apple Developer Program",
        "Developer ID Application",
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "KEYCHAIN_PASSWORD",
        "APPLE_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
        "workflow_dispatch",
        "Poly-macos-arm64",
        "Poly-macos-x86_64",
        "Applications",
        "Keychain",
        "~/Library/Application Support/Poly",
        "~/Library/Application Support/Poly/logs",
        "~/Library/Application Support/Poly/postgres.log",
        "Windows",
        "Intel",
    ):
        assert item in runbook
    assert re.search(r"(?i)stopp?ed|quit Poly|退出 Poly", runbook)
    assert re.search(r"(?i)no AWS runtime|不需要 AWS 运行时配置", runbook)
    assert re.search(r"(?i)installs? no other|无需安装.*运行时", runbook)
    assert re.search(r"(?i)not.*supported.*green native CI|绿色原生 CI.*支持", runbook)
    assert "repository secrets" in runbook
    assert "protected environment/repository secrets" not in runbook


def test_readme_links_to_the_macos_desktop_runbook() -> None:
    readme = read(README)
    assert "## macOS 桌面版" in readme
    assert "docs/runbooks/macos-desktop-build.md" in readme
