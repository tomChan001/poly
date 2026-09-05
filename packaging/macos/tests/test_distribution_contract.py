import json
import plistlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MACOS = ROOT / "packaging" / "macos"


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

    def test_verify_bundle_is_path_scoped_and_never_kills_by_name(self) -> None:
        script = self.read("verify-bundle.sh")
        self.assertIn("Contents/Resources/poly-runtime", script)
        self.assertIn("Contents/MacOS/Poly", script)
        self.assertIn('application id "com.poly.desktop"', script)
        self.assertNotRegex(script, r"\b(?:pkill|killall)\b")
        self.assertNotRegex(script, r"pgrep\s+(?:-[^ ]+\s+)*['\"]?Poly")
        self.assertIn("lsof -nP -a -p", script)
        self.assertIn("pids_at_exact_executable", script)
        self.assertIn("pids_under_path", script)
        self.assertIn('realpath "$1"', script)
        self.assertIn("127.0.0.1:", script)
        self.assertIn("refusing to mix verification with existing bundle processes", script)
        self.assertNotIn("mapfile", script)

    def test_notice_generator_has_deterministic_check_and_all_inventories(self) -> None:
        script = self.read("generate-notices.sh")
        self.assertIn("--check", script)
        self.assertIn("LC_ALL=C", script)
        for tool_or_input in (
            "cargo about",
            "cargo license",
            "pip-licenses",
            "package-lock.json",
            "CPython",
            "PyInstaller",
            "PostgreSQL",
            "Tauri",
            "WebKit",
        ):
            self.assertIn(tool_or_input, script)
        self.assertIn("uv export --frozen --no-dev", script)
        self.assertIn("--no-emit-project", script)
        self.assertIn('cd -- "${REPO_ROOT}"', script)
        self.assertIn('relative.startswith("postgres/")', script)
        self.assertIn('endswith(".so")', script)
        self.assertIn("frontend/node_modules", script)
        self.assertIn("npm_license_text", script)
        self.assertIn("differs from package-lock.json", script)
        self.assertIn("ignore-dev-dependencies = true", script)
        self.assertIn('--config "${WORK_DIR}/about.toml"', script)
        self.assertIn("cargo metadata --locked", script)
        self.assertIn('f"{relative}.LICENSE"', script)
        self.assertIn("packages_distributions", script)
        self.assertIn("stdlib_module_names", script)
        self.assertIn("macho-inventory", script)
        self.assertNotIn(".env", script)

    def test_notices_are_explicitly_preassembly_and_preserve_postgres_license(self) -> None:
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Pre-assembly notice", notices)
        self.assertIn("PostgreSQL License", notices)
        self.assertIn("IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA", notices)
        self.assertNotIn("complete inventory", notices.lower())


if __name__ == "__main__":
    unittest.main()
