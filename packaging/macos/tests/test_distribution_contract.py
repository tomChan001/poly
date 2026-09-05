import json
import importlib.util
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


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


class DistributionContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (MACOS / name).read_text(encoding="utf-8")

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
        bundle = config["bundle"]
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

    def test_dependency_checks_reject_unsafe_and_unknown_absolute_paths(self) -> None:
        for name in ("fetch-postgres.sh", "verify-bundle.sh"):
            script = self.read(name)
            for unsafe in ("/opt/homebrew", "/opt/local", "/Users/", "/runner/", "/home/"):
                self.assertIn(unsafe, script, name)
            self.assertIn("unexpected absolute dependency", script, name)
            self.assertIn("unexpected dependency or rpath", script, name)
            self.assertIn("otool -L", script, name)
            self.assertIn("otool -l", script, name)
            self.assertIn("LC_RPATH", script, name)
            self.assertNotIn("< <(", script, name)

    def test_macho_audit_propagates_find_and_otool_failures(self) -> None:
        bash = bash_executable()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
                [bash, str(MACOS / "fetch-postgres.sh"), "--audit-tree", str(root)],
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
                [bash, str(MACOS / "fetch-postgres.sh"), "--audit-tree", str(root)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(failed_otool.returncode, 0)
            self.assertIn("otool -L failed", failed_otool.stderr)

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
        module_path = MACOS / "license_inventory.py"
        spec = importlib.util.spec_from_file_location("license_inventory", module_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
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
                stdlib_modules=set(),
                native_license_root=native,
            )
            self.assertEqual(classifications[paths[1]], "PostgreSQL")
            self.assertEqual(classifications[paths[2]], "PostgreSQL")
            self.assertEqual(classifications[paths[3]], "CPython")
            with self.assertRaisesRegex(ValueError, "packaged native file lacks inventory"):
                module.classify_macho_paths(
                    ["_internal/libmystery.dylib"],
                    production_distributions={},
                    package_distributions={},
                    stdlib_modules=set(),
                    native_license_root=native,
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
        self.assertIn('normalized.startswith("postgres/")', helper)
        self.assertIn('endswith(".so")', helper)
        self.assertIn("npm_inventory", helper)
        self.assertIn("differs from lock inventory", helper)
        self.assertIn("cargo metadata --locked", script)
        self.assertIn('f"{relative}.LICENSE"', helper)
        self.assertIn("packages_distributions", helper)
        self.assertIn("stdlib_module_names", helper)
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
