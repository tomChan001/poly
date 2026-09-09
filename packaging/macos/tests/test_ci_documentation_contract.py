import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "macos-desktop.yml"
RUNBOOK = ROOT / "docs" / "runbooks" / "macos-desktop-build.md"
README = ROOT / "README.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_runtime_bakes_the_expected_apple_team_identity() -> None:
    workflow = read(WORKFLOW)

    assert "POLY_EXPECTED_PARENT_TEAM_ID" in workflow
    assert "secrets.APPLE_TEAM_ID" in workflow
    assert "ADHOC_VALIDATION_ONLY" in workflow


def test_workflow_is_read_only_and_uses_the_exact_native_matrix() -> None:
    workflow = read(WORKFLOW)
    assert re.search(
        r"(?m)^permissions:\s*\n\s+actions: read\s*\n\s+contents: read\s*$",
        workflow,
    )
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
    job_environment = workflow.split("    env:", 1)[1].split("    steps:", 1)[0]
    assert re.search(
        r"(?m)^\s+MACOSX_DEPLOYMENT_TARGET:\s*['\"]12\.0['\"]\s*$",
        job_environment,
    )


def test_workflow_uses_a_pinned_managed_python_with_its_license() -> None:
    workflow = read(WORKFLOW)
    job_environment = workflow.split("    env:", 1)[1].split("    steps:", 1)[0]

    assert re.search(
        r"(?m)^\s+UV_PYTHON:\s*['\"]3\.12\.11['\"]\s*$",
        job_environment,
    )
    assert re.search(
        r"(?m)^\s+UV_MANAGED_PYTHON:\s*['\"]1['\"]\s*$",
        job_environment,
    )
    install = 'uv python install "${UV_PYTHON}"'
    assert install in workflow
    assert workflow.index(install) < workflow.index("uv sync --frozen")


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
    action_pins = {
        "actions/checkout": ("11d5960a326750d5838078e36cf38b85af677262", "v4"),
        "actions/setup-node": ("49933ea5288caeca8642d1e84afbd3f7d6820020", "v4"),
        "astral-sh/setup-uv": ("d0d8abe699bfb85fec6de9f7adb5ae17292296ff", "v6"),
        "dtolnay/rust-toolchain": (
            "688313b0823df1393bcebb1b4add0438a6d36884",
            "1.88.0",
        ),
        "Swatinem/rust-cache": ("49a0bdc70d2e1b713ca9e2869b211fcce03d3c1c", "v2"),
        "actions/upload-artifact": (
            "ea165f8d65b6e75b540449e92b4886f43607fa02",
            "v4",
        ),
    }
    ordered_markers = (
        f"actions/checkout@{action_pins['actions/checkout'][0]}",
        f"actions/setup-node@{action_pins['actions/setup-node'][0]}",
        "npm ci",
        f"astral-sh/setup-uv@{action_pins['astral-sh/setup-uv'][0]}",
        "uv sync --frozen",
        "uv run --frozen ruff check .",
        "uv run --frozen pytest",
        "backend/tests/unit",
        "backend/tests/security",
        "backend/tests/integration/api/test_desktop_session.py",
        "packaging/macos/tests/test_distribution_contract.py",
        "packaging/macos/tests/test_ci_documentation_contract.py",
        "npm run lint",
        "npm test",
        "npm run build",
        "rustup target add",
        "cargo fmt --manifest-path src-tauri/Cargo.toml -- --check",
        "mkdir -p src-tauri/resources/poly-runtime",
        "cargo clippy --locked --manifest-path src-tauri/Cargo.toml",
        "cargo test --locked --manifest-path src-tauri/Cargo.toml",
        "packaging/macos/fetch-postgres.sh",
        "packaging/macos/build-runtime.sh",
        "packaging/macos/verify-runtime.sh",
        "cargo tauri build --target",
        "xcrun stapler",
        "packaging/macos/verify-bundle.sh",
        f"actions/upload-artifact@{action_pins['actions/upload-artifact'][0]}",
    )
    positions = [workflow.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    uses_entries = re.findall(
        r"(?m)^\s*-?\s*uses:\s*([^\s#]+)\s*(?:#\s*(\S+))?\s*$", workflow
    )
    assert uses_entries
    observed_counts: dict[str, int] = {}
    for uses, version_comment in uses_entries:
        owner, separator, revision = uses.partition("@")
        assert separator == "@"
        assert re.fullmatch(r"[0-9a-f]{40}", revision)
        assert owner in action_pins
        expected_revision, expected_version = action_pins[owner]
        assert revision == expected_revision
        assert version_comment == expected_version
        observed_counts[owner] = observed_counts.get(owner, 0) + 1
    assert observed_counts == {
        **{
            owner: 1
            for owner in action_pins
            if owner not in {"actions/checkout", "actions/upload-artifact"}
        },
        "actions/checkout": 2,
        "actions/upload-artifact": 2,
    }


def test_workflow_generates_exact_assembled_notices_for_the_bundle() -> None:
    workflow = read(WORKFLOW)
    stage = workflow.split(
        "- name: Stage complete runtime and generate assembled notices", 1
    )[1].split("- name: Install pinned Tauri CLI", 1)[0]

    assert "packaging/macos/generate-notices.sh --assembled" in stage
    assert "--check --assembled" not in stage


def test_non_release_build_audits_the_real_bundle_with_controlled_architecture() -> None:
    workflow = read(WORKFLOW)
    verification = workflow.split(
        "- name: Verify release bundle or ad-hoc contract", 1
    )[1].split("- name: Install DMG into an isolated home and smoke test", 1)[0]
    assert 'APP_PATH="${GITHUB_WORKSPACE}/' in verification
    assert (
        'packaging/macos/verify-bundle.sh --audit-bundle "${APP_PATH}"'
        in verification
    )
    assert "packaging/macos/verify-bundle.sh --check" not in verification
    assert "POLY_TARGET_ARCH" in workflow.split("    env:", 1)[1].split(
        "    steps:", 1
    )[0]


def test_smoke_uses_an_isolated_home_installed_dmg_and_command_guards() -> None:
    workflow = read(WORKFLOW)
    smoke = workflow.split(
        "- name: Install DMG into an isolated home and smoke test", 1
    )[1].split("- name: Remove temporary signing keychain", 1)[0]
    for marker in (
        "mktemp -d",
        "SMOKE_HOME",
        "SMOKE_APPLICATIONS",
        "hdiutil attach",
        "hdiutil detach",
        "ditto",
        "open -n",
        '"${RUNTIME_EXECUTABLE}" --keychain-smoke set --account',
        '"${RUNTIME_EXECUTABLE}" --keychain-smoke verify --account',
        '"${RUNTIME_EXECUTABLE}" --keychain-smoke delete --account',
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
    assert "uv run" not in smoke
    assert re.search(
        r"printf '%s' \"\$\{SMOKE_SECRET\}\"\s*\\?\s*\|\s*"
        r'"\$\{RUNTIME_EXECUTABLE\}" --keychain-smoke',
        smoke,
    )
    assert re.search(
        r"--keychain-smoke set.*?/usr/bin/open -n.*?tell application id.*?"
        r"/usr/bin/open -n.*?--keychain-smoke verify.*?--keychain-smoke delete",
        smoke,
        re.DOTALL,
    )


def test_smoke_uses_exact_runtime_evidence_without_unreliable_kernel_tracing() -> None:
    workflow = read(WORKFLOW)
    smoke = workflow.split(
        "- name: Install DMG into an isolated home and smoke test", 1
    )[1].split("- name: Remove temporary signing keychain", 1)[0]

    for marker in (
        "fs_usage",
        "CALIBRATION_",
        "APP_EXEC_TRACE",
        "TRACE_FAILURE",
        "start_exec_trace",
        "audit_exec_trace",
    ):
        assert marker not in smoke
    assert 'RUNTIME_EXECUTABLE="${RUNTIME_ROOT}/poly-runtime"' in smoke
    assert '"${GITHUB_WORKSPACE}/packaging/macos/verify-bundle.sh" ' in smoke
    assert '--enumerate-runtime "${RUNTIME_ROOT}/"' in smoke
    assert '/usr/sbin/lsof -nP -a -p "${pid}" -iTCP -sTCP:LISTEN' in smoke
    assert "TCP 127.0.0.1:" in smoke
    assert '[[ ! -s "${GUARD_LOG}" ]]' in smoke


def test_installed_dmg_smoke_has_step_timeout() -> None:
    workflow = read(WORKFLOW)
    smoke_header = workflow.split(
        "- name: Install DMG into an isolated home and smoke test", 1
    )[1].split("run: |", 1)[0]

    assert "timeout-minutes: 10" in smoke_header


def test_fast_resmoke_launches_directly_and_captures_failure_diagnostics() -> None:
    workflow = read(WORKFLOW)
    smoke = workflow.split(
        "- name: Re-smoke installed DMG with explicit phases", 1
    )[1]

    assert 'APP_EXECUTABLE="${INSTALLED_APP}/Contents/MacOS/Poly"' in smoke
    assert '"${APP_EXECUTABLE}" >>"${APP_STDOUT}" 2>>"${APP_STDERR}" &' in smoke
    assert "app_pid=$!" in smoke
    assert '--enumerate-runtime-from-pid "${RUNTIME_ROOT}/" "${root_pid}"' in smoke
    assert 'runtime_pids="$(enumerate_runtime "${app_pid}")"' in smoke
    assert "re-smoke process snapshot" in smoke
    assert "runtime.stderr.log" in smoke
    assert '/usr/bin/open -n "${INSTALLED_APP}"' not in smoke


def test_keychain_smoke_is_limited_to_signed_release_builds() -> None:
    workflow = read(WORKFLOW)
    smoke = workflow.split(
        "- name: Install DMG into an isolated home and smoke test", 1
    )[1].split("- name: Remove temporary signing keychain", 1)[0]
    release_guard = "if [[ \"${IS_RELEASE}\" == 'true' ]]; then"

    for marker in (
        '"${RUNTIME_EXECUTABLE}" --keychain-smoke set --account',
        '"${RUNTIME_EXECUTABLE}" --keychain-smoke verify --account',
        '"${RUNTIME_EXECUTABLE}" --keychain-smoke delete --account',
    ):
        marker_position = smoke.index(marker)
        guard_position = smoke.rfind(release_guard, 0, marker_position)
        end_position = smoke.find("\n          fi", marker_position)
        assert guard_position != -1
        assert end_position != -1


def test_validation_dmg_upload_survives_smoke_failure() -> None:
    workflow = read(WORKFLOW)
    upload_header = workflow.split(
        "- name: Upload architecture-specific DMG", 1
    )[1].split("uses:", 1)[0]

    assert (
        "if: ${{ !cancelled() && (success() || env.IS_RELEASE != 'true') }}"
        in upload_header
    )


def test_workflow_can_quickly_resmoke_an_existing_arm64_artifact() -> None:
    workflow = read(WORKFLOW)

    assert "arm64_artifact_run_id:" in workflow
    assert "resmoke-arm64-artifact:" in workflow
    assert 'gh run download "${{ inputs.arm64_artifact_run_id }}"' in workflow
    for phase in (
        "listener ready",
        "first shutdown complete",
        "single instance confirmed",
        "final shutdown complete",
    ):
        assert phase in workflow


def test_smoke_restores_trimmed_keychain_paths_or_fails_closed() -> None:
    workflow = read(WORKFLOW)
    for marker in (
        "KEYCHAIN_RESTORE_FAILURE",
        "restore_keychains",
        "keychain-restore-failure.log",
        "[[:space:]]",
    ):
        assert marker in workflow
    assert not re.search(
        r"security (?:default-keychain|list-keychains).*?\|\| true", workflow
    )
    assert re.search(
        r"if restore_keychains; then.*?security delete-keychain.*?else.*?"
        r"KEYCHAIN_RESTORE_FAILURE",
        workflow,
        re.DOTALL,
    )


def test_non_release_diagnostics_are_collected_and_uploaded_even_on_failure() -> None:
    workflow = read(WORKFLOW)
    assert "Collect sanitized validation diagnostics" in workflow
    assert "Upload validation diagnostics" in workflow
    assert re.search(
        r"Upload validation diagnostics.*?if:\s*\$\{\{\s*always\(\).*?"
        r"actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
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
