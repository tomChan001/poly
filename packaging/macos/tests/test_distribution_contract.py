import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
MACOS = ROOT / "packaging" / "macos"


def bash_executable() -> str:
    candidates = [
        os.environ.get("POLY_TEST_BASH"),
        str(Path.home() / "apps" / "Git" / "bin" / "bash.exe"),
        shutil.which("bash"),
        "/bin/bash",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        result = subprocess.run(
            [candidate, "--version"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and "GNU bash" in result.stdout:
            return candidate
    raise RuntimeError("GNU Bash is required for packaging script tests")


def write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def git_bash_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix().split(":", 1)[1]
    return f"/{drive}{tail}"


def load_macos_module(name: str):
    module_path = MACOS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(MACOS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class DistributionContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (MACOS / name).read_text(encoding="utf-8")

    def test_runtime_verifier_uses_pyinstaller_internal_postgres_layout(self) -> None:
        verifier = self.read("verify-runtime.sh")

        self.assertIn('RUNTIME_RESOURCE_ROOT="${BUNDLE_DIR}/_internal"', verifier)
        self.assertIn(
            'postgres_executable="${RUNTIME_RESOURCE_ROOT}/postgres/bin/${program}"',
            verifier,
        )
        self.assertIn(
            '"${RUNTIME_RESOURCE_ROOT}/postgres" '
            '"${RUNTIME_RESOURCE_ROOT}/postgres/bin/postgres"',
            verifier,
        )
        self.assertNotIn('"${BUNDLE_DIR}/postgres/bin/${program}"', verifier)

    def test_postgres_checksum_is_the_official_pin(self) -> None:
        self.assertEqual(
            self.read("postgres-SHA256SUMS"),
            "c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed  "
            "postgresql-16.15.tar.bz2\n",
        )

    def test_fetch_uses_only_pinned_official_source_and_verifies_before_extract(self) -> None:
        script = self.read("fetch-postgres.sh")
        url = "https://ftp.postgresql.org/pub/source/v16.15/postgresql-16.15.tar.bz2"
        self.assertEqual(script.count(url), 1)
        self.assertNotRegex(script, r"https?://(?!ftp\.postgresql\.org)")
        self.assertLess(script.index("shasum -a 256 -c"), script.index("tar -"))
        self.assertIn("set -euo pipefail", script)

    def test_fetch_supports_only_native_macos_triples(self) -> None:
        script = self.read("fetch-postgres.sh")
        self.assertIn("aarch64-apple-darwin", script)
        self.assertIn("x86_64-apple-darwin", script)
        self.assertIn("uname -s", script)
        self.assertIn("uname -m", script)
        self.assertNotIn("arm64-apple-darwin", script)

    def test_fetch_builds_only_world_binaries_with_minimal_features(self) -> None:
        script = self.read("fetch-postgres.sh")
        for token in (
            "--without-readline",
            "--without-zlib",
            "--disable-nls",
            "world-bin",
            "install-world-bin",
            "initdb postgres pg_isready psql createdb",
        ):
            self.assertIn(token, script)
        self.assertNotIn('cp -R -- "${INSTALL_PREFIX}/lib/postgresql"', script)
        self.assertIn("plpgsql.so", script)

    def test_empty_entitlements_have_no_forbidden_capabilities(self) -> None:
        data = (MACOS / "entitlements.plist").read_bytes()
        self.assertEqual(plistlib.loads(data), {})
        text = data.decode("utf-8")
        for key in (
            "app-sandbox",
            "allow-jit",
            "allow-unsigned-executable-memory",
            "network.server",
            "files.user-selected.read-write",
            "disable-library-validation",
        ):
            self.assertNotIn(key, text)

    def test_tauri_maps_complete_runtime_to_expected_bundle_layout(self) -> None:
        config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text())
        cargo = tomllib.loads(
            (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        )
        bundle = config["bundle"]
        self.assertEqual(config["mainBinaryName"], "Poly")
        self.assertNotIn("mainBinaryName", config["build"])
        self.assertEqual(
            cargo["bin"],
            [{"name": "Poly", "path": "src/main.rs"}],
        )
        self.assertEqual(bundle["targets"], ["app", "dmg"])
        self.assertEqual(
            bundle["resources"],
            {
                "resources/poly-runtime/": "poly-runtime/",
                "../THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
            },
        )
        self.assertEqual(
            bundle["macOS"]["entitlements"],
            "../packaging/macos/entitlements.plist",
        )
        self.assertIs(bundle["macOS"]["hardenedRuntime"], True)
        self.assertNotIn("signingIdentity", bundle["macOS"])

    def test_macos_distribution_scripts_pin_the_12_0_deployment_target(self) -> None:
        for name in (
            "fetch-postgres.sh",
            "verify-runtime.sh",
            "verify-bundle.sh",
        ):
            with self.subTest(name=name):
                script = self.read(name)
                self.assertIn('POLY_REQUIRED_MACOS_TARGET="12.0"', script)
                self.assertIn("MACOSX_DEPLOYMENT_TARGET", script)

    def test_macho_audit_rejects_fat_wrong_arch_new_or_missing_minos(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fake").write_bytes(b"Mach-O fixture")
            stubs = root / "stubs"
            stubs.mkdir()
            write_stub(stubs, "find", "printf '%s\\0' \"$1/fake\"")
            write_stub(stubs, "file", "printf 'Mach-O 64-bit executable\\n'")
            write_stub(
                stubs,
                "lipo",
                "[[ \"$1\" == '-archs' ]] || exit 2\n"
                "printf '%s\\n' \"${POLY_STUB_ARCHS:-x86_64}\"",
            )
            write_stub(
                stubs,
                "otool",
                "if [[ \"$1\" == '-L' ]]; then\n"
                "  printf '%s:\\n\\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\\n' \"$2\"\n"
                "elif [[ \"${POLY_STUB_NO_METADATA:-0}\" -eq 0 ]]; then\n"
                "  printf 'Load command 0\\n      cmd LC_BUILD_VERSION\\n  cmdsize 32\\n platform 1\\n    minos %s\\n      sdk 15.0\\n' \"${POLY_STUB_MINOS:-12.0}\"\n"
                "fi",
            )
            base_env = os.environ.copy()
            base_env.update(
                PATH=f"{git_bash_path(stubs)}:/usr/bin:/bin",
                POLY_TEST_FIND=git_bash_path(stubs / "find"),
                POLY_TEST_FILE=git_bash_path(stubs / "file"),
                POLY_TEST_LIPO=git_bash_path(stubs / "lipo"),
                POLY_TEST_OTOOL=git_bash_path(stubs / "otool"),
                POLY_TEST_EXPECTED_ARCH="x86_64",
                POLY_TEST_MAX_MIN_OS="12.0",
            )
            cases = (
                ({"POLY_STUB_ARCHS": "x86_64 arm64"}, "not thin"),
                ({"POLY_STUB_ARCHS": "arm64"}, "architecture mismatch"),
                ({"POLY_STUB_MINOS": "13.0"}, "deployment target"),
                ({"POLY_STUB_NO_METADATA": "1"}, "deployment target metadata"),
            )
            for overrides, expected_error in cases:
                with self.subTest(overrides=overrides):
                    env = base_env | overrides
                    result = subprocess.run(
                        [
                            bash,
                            str(MACOS / "verify-bundle.sh"),
                            "--audit-tree",
                            git_bash_path(root),
                        ],
                        capture_output=True,
                        text=True,
                        env=env,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(expected_error, result.stderr)

    def test_macho_audit_accepts_thin_arch_and_legacy_macos_metadata(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fake").write_bytes(b"Mach-O fixture")
            stubs = root / "stubs"
            stubs.mkdir()
            write_stub(stubs, "find", "printf '%s\\0' \"$1/fake\"")
            write_stub(stubs, "file", "printf 'Mach-O 64-bit executable\\n'")
            write_stub(stubs, "lipo", "printf 'x86_64\\n'")
            write_stub(
                stubs,
                "otool",
                "if [[ \"$1\" == '-L' ]]; then\n"
                "  printf '%s:\\n\\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\\n' \"$2\"\n"
                "else\n"
                "  printf 'Load command 0\\n      cmd LC_VERSION_MIN_MACOSX\\n  cmdsize 16\\n  version 10.13\\n      sdk 11.0\\n'\n"
                "fi",
            )
            env = os.environ.copy()
            env.update(
                PATH=f"{git_bash_path(stubs)}:/usr/bin:/bin",
                POLY_TEST_FIND=git_bash_path(stubs / "find"),
                POLY_TEST_FILE=git_bash_path(stubs / "file"),
                POLY_TEST_LIPO=git_bash_path(stubs / "lipo"),
                POLY_TEST_OTOOL=git_bash_path(stubs / "otool"),
                POLY_TEST_EXPECTED_ARCH="x86_64",
                POLY_TEST_MAX_MIN_OS="12.0",
            )
            result = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--audit-tree",
                    git_bash_path(root),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_audit_bundle_runs_real_bundle_checks_without_release_services(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "Poly.app"
            main = app / "Contents" / "MacOS" / "Poly"
            runtime = app / "Contents" / "Resources" / "poly-runtime" / "poly-runtime"
            for executable in (main, runtime):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b"Mach-O fixture")
                executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            stubs = root / "stubs"
            stubs.mkdir()
            write_stub(
                stubs,
                "find",
                "printf '%s\\0%s\\0' \"$1/MacOS/Poly\" \"$1/Resources/poly-runtime/poly-runtime\"",
            )
            write_stub(stubs, "file", "printf 'Mach-O 64-bit executable\\n'")
            write_stub(stubs, "lipo", "printf 'x86_64\\n'")
            write_stub(
                stubs,
                "otool",
                "if [[ \"$1\" == '-L' ]]; then\n"
                "  printf '%s:\\n\\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\\n' \"$2\"\n"
                "else\n"
                "  printf 'Load command 0\\n      cmd LC_BUILD_VERSION\\n  cmdsize 32\\n platform 1\\n    minos 12.0\\n      sdk 15.0\\n'\n"
                "fi",
            )
            codesign_log = root / "codesign.log"
            write_stub(
                stubs,
                "codesign",
                f"printf '%s\\n' \"$*\" >> '{git_bash_path(codesign_log)}'",
            )
            env = os.environ.copy()
            env.update(
                PATH=f"{git_bash_path(stubs)}:/usr/bin:/bin",
                MACOSX_DEPLOYMENT_TARGET="12.0",
                POLY_TARGET_ARCH="x86_64",
                POLY_TARGET_TRIPLE="x86_64-apple-darwin",
                POLY_TEST_CODESIGN=git_bash_path(stubs / "codesign"),
                POLY_TEST_FILE=git_bash_path(stubs / "file"),
                POLY_TEST_FIND=git_bash_path(stubs / "find"),
                POLY_TEST_LIPO=git_bash_path(stubs / "lipo"),
                POLY_TEST_OTOOL=git_bash_path(stubs / "otool"),
            )
            result = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--audit-bundle",
                    git_bash_path(app),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Bundle path, signature, and Mach-O audit passed", result.stdout)
            log = codesign_log.read_text(encoding="utf-8")
            self.assertIn("--verify --deep --strict", log)
            self.assertNotIn("stapler", log)
            self.assertNotIn("spctl", log)

    def test_dependency_checks_reject_unsafe_and_unknown_absolute_paths(self) -> None:
        audit = self.read("macho-audit.sh")
        for name in ("fetch-postgres.sh", "verify-bundle.sh"):
            script = self.read(name)
            self.assertIn('source "${SCRIPT_DIR}/macho-audit.sh"', script)
            self.assertNotIn("< <(", script + audit, name)
        for unsafe in ("/opt/homebrew", "/opt/local", "/Users/", "/runner/", "/home/"):
            self.assertIn(unsafe, audit)
        self.assertIn("nonrelocatable absolute dependency", audit)
        self.assertIn("unexpected dependency or rpath", audit)
        self.assertIn("otool -L", audit)
        self.assertIn("otool -l", audit)
        self.assertIn("LC_RPATH", audit)
        self.assertIn("realpath", audit)
        self.assertRegex(
            audit,
            r'\\\( -type f -o -type l \\\) -print0',
            "the real audit inventory must include symlinks so their targets are checked",
        )

    def test_macho_audit_propagates_find_and_otool_failures(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fake").write_bytes(b"Mach-O fixture")
            stubs = root / "stubs"
            stubs.mkdir()
            env = os.environ.copy()
            env["PATH"] = f"{git_bash_path(stubs)}:/usr/bin:/bin"
            env["POLY_TEST_FIND"] = git_bash_path(stubs / "find")
            env["POLY_TEST_FILE"] = git_bash_path(stubs / "file")
            env["POLY_TEST_OTOOL"] = git_bash_path(stubs / "otool")
            write_stub(stubs, "file", "printf 'data\\n'")
            write_stub(stubs, "otool", "exit 0")
            write_stub(stubs, "find", "exit 17")
            failed_find = subprocess.run(
                [
                    bash,
                    str(MACOS / "fetch-postgres.sh"),
                    "--audit-tree",
                    git_bash_path(root),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(failed_find.returncode, 0)
            self.assertIn("find failed", failed_find.stderr)

            write_stub(stubs, "find", "printf '%s\\0' \"$1/fake\"")
            write_stub(stubs, "file", "printf 'Mach-O 64-bit executable\\n'")
            write_stub(stubs, "otool", "exit 19")
            failed_otool = subprocess.run(
                [
                    bash,
                    str(MACOS / "fetch-postgres.sh"),
                    "--audit-tree",
                    git_bash_path(root),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(failed_otool.returncode, 0)
            self.assertIn("otool -L failed", failed_otool.stderr)

    def test_macho_audit_resolves_install_names_and_rejects_escape_or_missing(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            tree = fixture / "tree"
            owner = tree / "bin" / "owner"
            library = tree / "lib" / "libok.dylib"
            escaped = fixture / "escape.dylib"
            for path in (owner, library, escaped):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"Mach-O fixture")
            stubs = fixture / "stubs"
            stubs.mkdir()
            write_stub(stubs, "find", "printf '%s\\0' \"$1/bin/owner\"")
            write_stub(stubs, "file", "printf 'Mach-O 64-bit executable\\n'")
            env = os.environ.copy()
            env["PATH"] = f"{git_bash_path(stubs)}:/usr/bin:/bin"
            env.update(
                POLY_TEST_FIND=git_bash_path(stubs / "find"),
                POLY_TEST_FILE=git_bash_path(stubs / "file"),
                POLY_TEST_OTOOL=git_bash_path(stubs / "otool"),
            )

            cases = [
                (
                    "@loader_path/../../escape.dylib",
                    "",
                    "escapes packaged root",
                ),
                (
                    "@rpath/missing.dylib",
                    "@loader_path/../lib",
                    "unresolved @rpath dependency",
                ),
                (
                    git_bash_path(library),
                    "",
                    "nonrelocatable absolute dependency",
                ),
            ]
            for dependency, rpath, expected_error in cases:
                with self.subTest(dependency=dependency):
                    rpath_output = (
                        "printf '          cmd LC_RPATH\\n"
                        "      cmdsize 48\\n"
                        f"         path {rpath} (offset 12)\\n'"
                        if rpath
                        else ":"
                    )
                    write_stub(
                        stubs,
                        "otool",
                        "if [[ \"$1\" == '-L' ]]; then "
                        f"printf '%s:\\n\\t{dependency} (compatibility version 1.0.0)\\n' \"$2\"; "
                        f"else {rpath_output}; fi",
                    )
                    for script_name in ("fetch-postgres.sh", "verify-bundle.sh"):
                        result = subprocess.run(
                            [
                                bash,
                                str(MACOS / script_name),
                                "--audit-tree",
                                str(tree),
                            ],
                            capture_output=True,
                            text=True,
                            env=env,
                            check=False,
                        )
                        self.assertNotEqual(result.returncode, 0, script_name)
                        self.assertIn(expected_error, result.stderr, script_name)

            write_stub(
                stubs,
                "otool",
                "if [[ \"$1\" == '-L' ]]; then "
                "printf '%s:\\n\\t@rpath/libok.dylib (compatibility version 1.0.0)\\n' \"$2\"; "
                "else printf '          cmd LC_RPATH\\n      cmdsize 48\\n"
                "         path @loader_path/../lib (offset 12)\\n'; fi",
            )
            for script_name in ("fetch-postgres.sh", "verify-bundle.sh"):
                result = subprocess.run(
                    [
                        bash,
                        str(MACOS / script_name),
                        "--audit-tree",
                        git_bash_path(tree),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_macho_audit_uses_main_executable_context(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            tree = fixture / "tree"
            owner = tree / "Frameworks" / "Nested" / "owner.dylib"
            library = tree / "Frameworks" / "libok.dylib"
            main_executable = tree / "MacOS" / "Poly"
            for path in (owner, library, main_executable):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"Mach-O fixture")
            stubs = fixture / "stubs"
            stubs.mkdir()
            write_stub(stubs, "find", "printf '%s\\0' \"$1/Frameworks/Nested/owner.dylib\"")
            write_stub(stubs, "file", "printf 'Mach-O 64-bit dynamically linked shared library\\n'")
            write_stub(
                stubs,
                "otool",
                "if [[ \"$1\" == '-L' ]]; then "
                "printf '%s:\\n\\t@executable_path/../Frameworks/libok.dylib "
                "(compatibility version 1.0.0)\\n' \"$2\"; else :; fi",
            )
            env = os.environ.copy()
            env["PATH"] = f"{git_bash_path(stubs)}:/usr/bin:/bin"
            env.update(
                POLY_TEST_FIND=git_bash_path(stubs / "find"),
                POLY_TEST_FILE=git_bash_path(stubs / "file"),
                POLY_TEST_OTOOL=git_bash_path(stubs / "otool"),
                POLY_TEST_MAIN_EXECUTABLE=git_bash_path(main_executable),
            )
            for script_name in ("fetch-postgres.sh", "verify-bundle.sh"):
                result = subprocess.run(
                    [
                        bash,
                        str(MACOS / script_name),
                        "--audit-tree",
                        git_bash_path(tree),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_macho_audit_rejects_tree_outside_declared_boundary(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            boundary = fixture / "Poly.app"
            tree = fixture / "escaped-contents"
            owner = tree / "owner"
            for directory in (boundary, tree):
                directory.mkdir()
            owner.write_bytes(b"not Mach-O")
            stubs = fixture / "stubs"
            stubs.mkdir()
            write_stub(stubs, "find", "printf '%s\\0' \"$1/owner\"")
            write_stub(stubs, "file", "printf 'data\\n'")
            write_stub(stubs, "otool", "exit 0")
            env = os.environ.copy()
            env["PATH"] = f"{git_bash_path(stubs)}:/usr/bin:/bin"
            env.update(
                POLY_TEST_FIND=git_bash_path(stubs / "find"),
                POLY_TEST_FILE=git_bash_path(stubs / "file"),
                POLY_TEST_OTOOL=git_bash_path(stubs / "otool"),
                POLY_TEST_AUDIT_BOUNDARY=git_bash_path(boundary),
            )
            result = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--audit-tree",
                    git_bash_path(tree),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("audit boundary", result.stderr)

    def test_process_enumeration_failure_is_fatal(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            stubs = Path(temporary) / "stubs"
            stubs.mkdir()
            write_stub(stubs, "ps", "exit 23")
            env = os.environ.copy()
            env["PATH"] = f"{git_bash_path(stubs)}:/usr/bin:/bin"
            env["POLY_TEST_PS"] = git_bash_path(stubs / "ps")
            result = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--enumerate-runtime",
                    "/tmp/poly-runtime/",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("process enumeration failed", result.stderr)

            write_stub(
                stubs,
                "ps",
                "if [[ \"$*\" == *'-axo'* ]]; then "
                "printf '4242 /tmp/poly-runtime/poly-runtime\\n'; "
                "else printf ' 4242\\n'; fi",
            )
            write_stub(stubs, "lsof", "printf 'injected lsof failure\\n' >&2; exit 24")
            env["POLY_TEST_LSOF"] = git_bash_path(stubs / "lsof")
            failed_lsof = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--enumerate-runtime",
                    "/tmp/poly-runtime/",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(failed_lsof.returncode, 0)
            self.assertRegex(
                failed_lsof.stderr,
                r"(?:exact executable|path-scoped process) enumeration failed",
            )

    def test_runtime_license_classifier_handles_pyinstaller_internal_layout(self) -> None:
        module = load_macos_module("license_inventory")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / "native-licenses"
            native.mkdir()
            runtime = root / "poly-runtime"
            paths = [
                "poly-runtime",
                "_internal/postgres/bin/postgres",
                "postgres/bin/psql",
                "_internal/libpython3.12.dylib",
                "libpython3.12.dylib",
            ]
            for relative in paths:
                packaged = runtime / relative
                packaged.parent.mkdir(parents=True, exist_ok=True)
                packaged.write_bytes(b"Mach-O fixture")
            macho_inventory = root / "macho-inventory.txt"
            macho_inventory.write_text("\n".join(paths) + "\n", encoding="utf-8")
            classifications = module.classify_macho_paths(
                macho_inventory.read_text(encoding="utf-8").splitlines(),
                production_distributions={},
                package_distributions={},
                distribution_files={},
                stdlib_files=set(),
                cpython_library_files={
                    "libpython3.12.dylib",
                    "_internal/libpython3.12.dylib",
                },
                native_license_root=native,
            )
            self.assertEqual(classifications[paths[1]], "PostgreSQL")
            self.assertEqual(classifications[paths[2]], "PostgreSQL")
            self.assertEqual(classifications[paths[3]], "CPython")
            self.assertEqual(classifications[paths[4]], "CPython")
            with self.assertRaisesRegex(ValueError, "packaged native file lacks inventory"):
                module.classify_macho_paths(
                    ["_internal/libmystery.dylib"],
                    production_distributions={},
                    package_distributions={},
                    distribution_files={},
                    stdlib_files=set(),
                    cpython_library_files={
                        "libpython3.12.dylib",
                        "_internal/libpython3.12.dylib",
                    },
                    native_license_root=native,
                )
            with self.assertRaisesRegex(ValueError, "packaged native file lacks inventory"):
                module.classify_macho_paths(
                    ["_internal/libpython9.9.dylib"],
                    production_distributions={},
                    package_distributions={},
                    distribution_files={},
                    stdlib_files=set(),
                    cpython_library_files={
                        "libpython3.12.dylib",
                        "_internal/libpython3.12.dylib",
                    },
                    native_license_root=native,
                )
            with self.assertRaisesRegex(ValueError, "packaged native file lacks inventory"):
                module.classify_macho_paths(
                    ["_internal/spoof/libpython3.12.dylib"],
                    production_distributions={},
                    package_distributions={},
                    distribution_files={},
                    stdlib_files=set(),
                    cpython_library_files={
                        "libpython3.12.dylib",
                        "_internal/libpython3.12.dylib",
                    },
                    native_license_root=native,
                )

    def test_cpython_library_manifest_has_both_exact_onedir_layouts(self) -> None:
        module = load_macos_module("notice_generator")
        values = {
            "LDLIBRARY": "/Library/Frameworks/Python/libpython3.12.dylib",
            "INSTSONAME": "libpython3.12.dylib",
        }
        with patch.object(
            module.sysconfig,
            "get_config_var",
            side_effect=values.get,
        ):
            self.assertEqual(
                module.cpython_library_files(),
                frozenset(
                    {
                        "libpython3.12.dylib",
                        "_internal/libpython3.12.dylib",
                    }
                ),
            )

    def test_runtime_file_inventory_fails_unknown_wasm_script_and_blob(self) -> None:
        module = load_macos_module("license_inventory")
        production = {"certifi": {"license": "MPL-2.0"}}
        known = module.classify_runtime_paths(
            [
                "alembic.ini",
                "frontend/dist/index.html",
                "_internal/base_library.zip",
                "_internal/certifi/cacert.pem",
                "_internal/json/__init__.py",
                "_internal/pyimod01_archive.pyc",
            ],
            macho_classifications={},
            production_distributions=production,
            package_distributions={"certifi": ["certifi"]},
            distribution_files={"certifi": {"certifi/cacert.pem"}},
            stdlib_files={"json/__init__.py"},
            pyinstaller_runtime_files={"pyimod01_archive.pyc"},
        )
        self.assertEqual(known["alembic.ini"], "Poly application asset")
        self.assertIn("certifi (MPL-2.0)", known["_internal/certifi/cacert.pem"])
        for unknown in (
            "_internal/mystery.wasm",
            "hooks/start.sh",
            "blob.bin",
            "_internal/certifi/untracked.wasm",
            "_internal/json/untracked.bin",
            "_internal/pyimod_untrusted.sh",
        ):
            with self.subTest(unknown=unknown), self.assertRaisesRegex(
                ValueError, "packaged file lacks license inventory"
            ):
                module.classify_runtime_paths(
                    [unknown],
                    macho_classifications={},
                    production_distributions=production,
                    package_distributions={"certifi": ["certifi"]},
                    distribution_files={"certifi": {"certifi/cacert.pem"}},
                    stdlib_files={"json/__init__.py"},
                    pyinstaller_runtime_files={"pyimod01_archive.pyc"},
                )

    def test_listener_audit_rejects_mixed_wildcard_and_postgres_tcp(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            runtime = fixture / "runtime"
            runtime_executable = runtime / "poly-runtime"
            postgres_executable = runtime / "_internal/postgres/bin/postgres"
            for path in (runtime_executable, postgres_executable):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"executable")
            stubs = fixture / "stubs"
            stubs.mkdir()
            lsof_path = stubs / "lsof"
            env = os.environ.copy()
            env["PATH"] = f"{git_bash_path(stubs)}:/usr/bin:/bin"
            env["POLY_TEST_LSOF"] = git_bash_path(lsof_path)
            runtime_argument = git_bash_path(runtime) + "/"

            write_stub(
                stubs,
                "lsof",
                "if [[ \"$*\" == *'-d txt'* ]]; then "
                f"printf 'n{git_bash_path(runtime_executable)}\\n'; "
                "else printf 'n127.0.0.1:43123\\nn*:43123\\n'; fi",
            )
            mixed = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--audit-listeners",
                    runtime_argument,
                    "4242",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(mixed.returncode, 0)
            self.assertIn("non-loopback TCP listener", mixed.stderr)

            write_stub(
                stubs,
                "lsof",
                "if [[ \"$*\" == *'-d txt'* ]]; then "
                f"printf 'n{git_bash_path(postgres_executable)}\\n'; "
                "else printf 'n127.0.0.1:5432\\n'; fi",
            )
            postgres = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--audit-listeners",
                    runtime_argument,
                    "4343",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(postgres.returncode, 0)
            self.assertIn("PostgreSQL process exposed a TCP listener", postgres.stderr)

            write_stub(
                stubs,
                "lsof",
                "if [[ \"$*\" == *'-d txt'* ]]; then "
                f"printf 'n{git_bash_path(runtime_executable)}\\n'; "
                "else printf 'n127.0.0.1:43123\\nn[::1]:43123\\n'; fi",
            )
            loopback_only = subprocess.run(
                [
                    bash,
                    str(MACOS / "verify-bundle.sh"),
                    "--audit-listeners",
                    runtime_argument,
                    "4444",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(loopback_only.returncode, 0, loopback_only.stderr)

    def test_external_license_inventory_rejects_conflicting_expression(self) -> None:
        module = load_macos_module("notice_generator")
        expected = [
            {
                "name": "example-package",
                "version": "1.0.0",
                "license": "MIT OR Apache-2.0",
                "source": "locked",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "licenses.json"
            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "GPL-3.0-only",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "license mismatch"):
                module.validate_external_inventory(
                    report, expected, ecosystem="Python"
                )

            expected[0]["license"] = "MIT AND Apache-2.0"
            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "MIT OR Apache-2.0",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "license mismatch"):
                module.validate_external_inventory(
                    report, expected, ecosystem="Python"
                )

            expected[0]["license"] = "Apache-2.0 WITH LLVM-exception"
            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "Apache-2.0",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "license mismatch"):
                module.validate_external_inventory(
                    report, expected, ecosystem="Python"
                )

            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "Apache-2.0 WITH LLVM-exception",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            module.validate_external_inventory(report, expected, ecosystem="Python")
            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "Apache-2.0 WITH Classpath-exception-2.0",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "license mismatch"):
                module.validate_external_inventory(
                    report, expected, ecosystem="Python"
                )

            expected[0]["license"] = "MIT AND (Apache-2.0 OR BSD-2-Clause)"
            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "(MIT AND Apache-2.0) OR BSD-2-Clause",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "license mismatch"):
                module.validate_external_inventory(
                    report, expected, ecosystem="Python"
                )

            expected[0]["license"] = "BSD-2-Clause"
            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "BSD-3-Clause",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "license mismatch"):
                module.validate_external_inventory(
                    report, expected, ecosystem="Python"
                )

            expected[0]["license"] = "MIT OR Apache-2.0"
            report.write_text(
                json.dumps(
                    [
                        {
                            "Name": "example-package",
                            "Version": "1.0.0",
                            "License": "Apache Software License; MIT License",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            module.validate_external_inventory(report, expected, ecosystem="Python")

    def test_notice_generator_imports_with_repo_packaging_namespace(self) -> None:
        module = load_macos_module("notice_generator")
        with tempfile.TemporaryDirectory() as temporary:
            requirements = Path(temporary) / "requirements.txt"
            requirements.write_text(
                'example-package==1.0; sys_platform == "darwin"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                module.production_requirement_names(requirements),
                {"example-package"},
            )

    def test_verify_bundle_is_path_scoped_and_never_kills_by_name(self) -> None:
        script = self.read("verify-bundle.sh")
        self.assertIn("Contents/Resources/poly-runtime", script)
        self.assertIn("Contents/MacOS/Poly", script)
        self.assertIn('application id "com.poly.desktop"', script)
        self.assertNotRegex(script, r"\b(?:pkill|killall)\b")
        self.assertNotRegex(script, r"pgrep\s+(?:-[^ ]+\s+)*['\"]?Poly")
        self.assertIn('-d txt -Fn', script)
        self.assertIn('-ww -axo pid=,args=', script)
        self.assertIn('+D "${runtime_directory}" -Fp', script)
        self.assertIn("pids_at_exact_executable", script)
        self.assertIn("pids_under_path", script)
        self.assertIn('realpath "$1"', script)
        self.assertIn("127.0.0.1:", script)
        self.assertIn("refusing to mix verification with existing bundle processes", script)
        self.assertNotIn("mapfile", script)

    def test_notice_generator_has_deterministic_check_and_all_inventories(self) -> None:
        script = self.read("generate-notices.sh")
        helper = self.read("notice_generator.py") + self.read("license_inventory.py")
        self.assertIn("--check", script)
        self.assertIn("LC_ALL=C", script)
        for tool_or_input in (
            "cargo license",
            "pip-licenses",
            "package-lock.json",
            "CPython",
            "PyInstaller",
            "PostgreSQL",
            "Tauri",
            "WebKit",
        ):
            self.assertIn(tool_or_input, script + helper)
        self.assertIn("uv export --frozen --no-dev", script)
        self.assertIn("--no-emit-project", script)
        self.assertIn('cd -- "${REPO_ROOT}"', script)
        self.assertIn('bundled_path.startswith("postgres/")', helper)
        self.assertIn('endswith(".so")', helper)
        self.assertIn("npm_inventory", helper)
        self.assertIn("differs from lock inventory", helper)
        self.assertIn("cargo metadata --locked", script)
        self.assertIn('f"{relative}.LICENSE"', helper)
        self.assertIn("packages_distributions", helper)
        self.assertIn("cpython_stdlib_files", helper)
        self.assertIn("distribution.files", helper)
        self.assertIn("pyinstaller_runtime_files", helper)
        self.assertIn("macho-inventory", script)
        self.assertNotIn(".env", script + helper)

    def test_committed_notices_are_generated_and_checkable_without_bundle(self) -> None:
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertNotIn("Pre-assembly notice", notices)
        self.assertIn("Generated deterministically", notices)
        self.assertIn("PostgreSQL License", notices)
        self.assertIn("IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA", notices)
        result = subprocess.run(
            [bash_executable(), str(MACOS / "generate-notices.sh"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
