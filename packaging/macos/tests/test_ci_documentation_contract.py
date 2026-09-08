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
        **{owner: 1 for owner in action_pins if owner != "actions/upload-artifact"},
        "actions/upload-artifact": 2,
    }


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


def test_smoke_uses_calibrated_kernel_exec_tracing() -> None:
    workflow = read(WORKFLOW)
    assert (
        'BUNDLED_POSTGRES="${RUNTIME_ROOT}/_internal/postgres/bin/postgres"'
        in workflow
    )
    assert 'postgres_bin="${RUNTIME_ROOT}/_internal/postgres/bin/"' in workflow
    assert 'BUNDLED_POSTGRES="${RUNTIME_ROOT}/postgres/bin/postgres"' not in workflow
    for marker in (
        "/usr/bin/sudo -n /usr/bin/fs_usage -w -f exec",
        '"/usr/bin/python3"',
        "CALIBRATION_TRACE",
        "CALIBRATION_PARENT",
        "/usr/bin/clang",
        "posix_spawn",
        "APP_EXEC_TRACE",
        "Poly poly-runtime",
        "audit_exec_trace",
        "exec-trace-failures.log",
        "${RUNTIME_ROOT}/_internal/postgres/bin/",
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
        assert forbidden_command in workflow
    assert "/bin/ps -ww -axo pid=,ppid=,comm=" not in workflow
    assert "audit_app_processes" not in workflow
    assert workflow.count('start_exec_trace "${') == 2
    assert (
        workflow.count('start_exec_trace "${CALIBRATION_TRACE}" Poly poly-runtime') == 1
    )
    assert workflow.count('start_exec_trace "${APP_EXEC_TRACE}" Poly poly-runtime') == 1
    assert re.search(
        r"start_exec_trace.*?CALIBRATION_PARENT.*?stop_exec_trace.*?"
        r"grep.*?/usr/bin/python3",
        workflow,
        re.DOTALL,
    )
    assert '[[ -s "${APP_EXEC_TRACE}" ]]' in workflow
    assert re.search(
        r"grep -Fq.*?RUNTIME_EXECUTABLE.*?APP_EXEC_TRACE.*?\|\|.*?"
        r"grep -Fq.*?BUNDLED_POSTGRES.*?APP_EXEC_TRACE",
        workflow,
        re.DOTALL,
    )
    assert '[[ ! -s "${TRACE_FAILURE}" ]]' in workflow
    smoke = workflow.split(
        "- name: Install DMG into an isolated home and smoke test", 1
    )[1].split("- name: Remove temporary signing keychain", 1)[0]
    trace_start = smoke.index(
        'start_exec_trace "${APP_EXEC_TRACE}" Poly poly-runtime'
    )
    app_launch = smoke.index('/usr/bin/open -n "${INSTALLED_APP}"', trace_start)
    trace_stop = smoke.index("stop_exec_trace", app_launch)
    trace_audit = smoke.index(
        'audit_exec_trace "${APP_EXEC_TRACE}" "${TRACE_FAILURE}"', trace_stop
    )
    positive_evidence = smoke.index(
        'grep -Fq "${RUNTIME_EXECUTABLE}" "${APP_EXEC_TRACE}"', trace_audit
    )
    keychain_verify = smoke.index("--keychain-smoke verify", trace_start)
    keychain_delete = smoke.index("--keychain-smoke delete", trace_start)
    assert (
        trace_start
        < app_launch
        < trace_stop
        < trace_audit
        < positive_evidence
        < keychain_verify
        < keychain_delete
    )
    trace_window = smoke[trace_start:trace_stop]
    assert "--keychain-smoke verify" not in trace_window
    assert "--keychain-smoke delete" not in trace_window
    post_trace_window = smoke[trace_stop:]
    assert "assert_exec_trace_alive" not in post_trace_window


def test_exec_trace_unexpected_exit_is_a_hard_failure() -> None:
    workflow = read(WORKFLOW)
    assert "assert_exec_trace_alive" in workflow
    assert "fs_usage exited before intentional stop" in workflow
    assert workflow.count("assert_exec_trace_alive") >= 4
    assert "trace_stop_requested=1" in workflow
    assert "! /bin/kill -0" not in workflow
    assert "if /bin/kill -0" not in workflow
    assert workflow.count("/usr/bin/sudo -n /bin/kill -0") == 1
    assert workflow.count("trace_is_alive") >= 5
    for marker in (
        "bounded_stop_and_reap",
        "wait_for_trace_exit",
        "reap_confirmed_trace",
        "fs_usage liveness probe failed",
        "failed to send fs_usage signal",
        "for ((trace_wait = 1; trace_wait <=",
    ):
        assert marker in workflow
    stop = workflow.split("bounded_stop_and_reap()", 1)[1].split(
        "assert_exec_trace_alive()", 1
    )[0]
    assert "if ! trace_is_alive" not in stop
    assert "trace_is_alive || probe_status=$?" in stop
    assert "wait_for_trace_exit || wait_status=$?" in stop
    assert "for signal_name in INT TERM KILL" in stop
    assert workflow.count('wait "${trace_pid}"') == 1
    reap = workflow.split("reap_confirmed_trace()", 1)[1].split(
        "bounded_stop_and_reap()", 1
    )[0]
    assert 'wait "${trace_pid}" || true' in reap
    unexpected = workflow.split("assert_exec_trace_alive()", 1)[1].split(
        "start_exec_trace()", 1
    )[0]
    assert "bounded_stop_and_reap" in unexpected


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
