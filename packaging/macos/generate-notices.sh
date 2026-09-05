#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
readonly OUTPUT="${REPO_ROOT}/THIRD_PARTY_NOTICES.md"

die() {
  printf 'generate-notices: %s\n' "$*" >&2
  exit 1
}

MODE="generate"
case "${1:-}" in
  "") ;;
  --check) MODE="check" ;;
  --static-check)
    [[ -f "${REPO_ROOT}/src-tauri/Cargo.lock" ]] || die "missing locked Rust dependency input"
    [[ -f "${REPO_ROOT}/uv.lock" ]] || die "missing locked Python dependency input"
    [[ -f "${REPO_ROOT}/frontend/package-lock.json" ]] || die "missing npm production package-lock.json"
    [[ -f "${OUTPUT}" ]] || die "missing committed notices"
    printf 'Notice generator inputs and committed pre-assembly notice are present.\n'
    exit 0
    ;;
  *) die "usage: $0 [--check|--static-check]" ;;
esac
[[ "$#" -le 1 ]] || die "usage: $0 [--check|--static-check]"
cd -- "${REPO_ROOT}"

readonly TARGET_TRIPLE="${POLY_TARGET_TRIPLE:-}"
case "${TARGET_TRIPLE}" in
  aarch64-apple-darwin|x86_64-apple-darwin) ;;
  *) die "set POLY_TARGET_TRIPLE to an assembled macOS architecture before generating release notices" ;;
esac

readonly RUNTIME_DIR="${REPO_ROOT}/src-tauri/resources/poly-runtime"
readonly POSTGRES_DIR="${SCRIPT_DIR}/postgres/${TARGET_TRIPLE}"
readonly NPM_ROOT="${REPO_ROOT}/frontend/node_modules"
[[ -x "${RUNTIME_DIR}/poly-runtime" ]] || die "assemble the complete PyInstaller onedir tree first: ${RUNTIME_DIR}"
[[ -f "${POSTGRES_DIR}/COPYRIGHT" ]] || die "assemble PostgreSQL (including COPYRIGHT) first: ${POSTGRES_DIR}"
[[ -d "${NPM_ROOT}" ]] || die "npm production license files are unavailable; run 'npm --prefix frontend ci' first: ${NPM_ROOT}"

for command_name in cargo file npm uv; do
  command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
done
uv run --frozen pip-licenses --version >/dev/null 2>&1 ||
  die "pip-licenses is unavailable; run 'uv sync --frozen' before generating notices"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/poly-notices.XXXXXXXX")"
cleanup() {
  [[ -n "${WORK_DIR:-}" && -d "${WORK_DIR}" ]] && rm -rf -- "${WORK_DIR}"
}
trap cleanup EXIT INT TERM

RUST_REPORT="${WORK_DIR}/rust.md"
cargo metadata --locked --manifest-path "${REPO_ROOT}/src-tauri/Cargo.toml" --format-version 1 >/dev/null
if cargo about --version >/dev/null 2>&1; then
  printf 'ignore-dev-dependencies = true\n' >"${WORK_DIR}/about.toml"
  cat >"${WORK_DIR}/about.hbs" <<'EOF'
{{#each licenses}}
### {{name}}

Used by:{{#each used_by}} `{{crate.name}} {{crate.version}}`{{/each}}

{{text}}

{{/each}}
EOF
  cargo about generate --config "${WORK_DIR}/about.toml" --manifest-path "${REPO_ROOT}/src-tauri/Cargo.toml" \
    "${WORK_DIR}/about.hbs" >"${RUST_REPORT}"
elif cargo license --version >/dev/null 2>&1; then
  {
    printf '> License expressions below were produced by cargo license; install cargo-about in release CI to embed full license texts.\n\n'
    cargo license --manifest-path "${REPO_ROOT}/src-tauri/Cargo.toml" --avoid-dev-deps --tsv
  } >"${RUST_REPORT}"
else
  die "install cargo-about or cargo-license to inventory locked production Rust dependencies"
fi

uv run --frozen pip-licenses --format=json --with-license-file --no-license-path \
  >"${WORK_DIR}/python.json"
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt \
  >"${WORK_DIR}/python-production.txt"

CPYTHON_LICENSE="${POLY_CPYTHON_LICENSE:-}"
if [[ -z "${CPYTHON_LICENSE}" ]]; then
  CPYTHON_LICENSE="$(uv run --frozen python - <<'PY'
import pathlib, sys
for root in (pathlib.Path(sys.base_prefix), pathlib.Path(sys.prefix)):
    for name in ("LICENSE.txt", "LICENSE"):
        candidate = root / name
        if candidate.is_file():
            print(candidate)
            raise SystemExit
PY
)"
fi
[[ -f "${CPYTHON_LICENSE}" ]] || die "CPython license not found; set POLY_CPYTHON_LICENSE to the license shipped with the embedded interpreter"

PYINSTALLER_LICENSE="$(uv run --frozen python - <<'PY'
import pathlib
import PyInstaller
root = pathlib.Path(PyInstaller.__file__).resolve().parent
for candidate in (root / "COPYING.txt", *sorted(root.parent.glob("pyinstaller-*.dist-info/licenses/COPYING.txt"))):
    if candidate.is_file():
        print(candidate)
        raise SystemExit
PY
)"
[[ -f "${PYINSTALLER_LICENSE}" ]] || die "PyInstaller bootloader license could not be located in the frozen build environment"

NATIVE_LICENSE_DIR="${SCRIPT_DIR}/native-licenses"
MACHO_INVENTORY="${WORK_DIR}/macho-inventory.txt"
while IFS= read -r -d '' candidate; do
  file -b -- "${candidate}" | grep -q 'Mach-O' || continue
  relative="${candidate#"${RUNTIME_DIR}/"}"
  printf '%s\n' "${relative}"
done < <(find "${RUNTIME_DIR}" -type f -print0) >"${MACHO_INVENTORY}"

GENERATED="${WORK_DIR}/THIRD_PARTY_NOTICES.md"
uv run --frozen python - "${RUST_REPORT}" "${WORK_DIR}/python.json" "${WORK_DIR}/python-production.txt" \
  "${REPO_ROOT}/frontend/package-lock.json" "${NPM_ROOT}" "${CPYTHON_LICENSE}" \
  "${PYINSTALLER_LICENSE}" "${POSTGRES_DIR}/COPYRIGHT" "${RUNTIME_DIR}" \
  "${POSTGRES_DIR}" "${NATIVE_LICENSE_DIR}" "${MACHO_INVENTORY}" "${GENERATED}" <<'PY'
import importlib.metadata, json, pathlib, re, sys

rust_path, python_path, production_path, npm_path, npm_root, cpython_path, pyinstaller_path, postgres_path, runtime_path, pg_root, native_root, macho_path, output_path = map(pathlib.Path, sys.argv[1:])

def clean(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()

def fenced(text):
    return "```text\n" + clean(text) + "\n```"

def normalized_name(name):
    return re.sub(r"[-_.]+", "-", name).casefold()

production_names = set()
for line in production_path.read_text(encoding="utf-8").splitlines():
    match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[.*?\])?(?:==|\s*@)", line.strip())
    if match:
        production_names.add(normalized_name(match.group(1)))
python_packages = [
    package for package in json.loads(python_path.read_text(encoding="utf-8"))
    if normalized_name(package.get("Name", "")) in production_names
]
python_packages.sort(key=lambda p: (p.get("Name", "").casefold(), p.get("Version", "")))
production_distributions = {normalized_name(package.get("Name", "")): package for package in python_packages}
package_distributions = importlib.metadata.packages_distributions()
lock = json.loads(npm_path.read_text(encoding="utf-8"))
npm_packages = []
for package_path, package in lock.get("packages", {}).items():
    if not package_path or package.get("dev") is True:
        continue
    name = package.get("name") or package_path.rsplit("node_modules/", 1)[-1]
    package_dir = npm_root / package_path.removeprefix("node_modules/")
    installed_manifest = package_dir / "package.json"
    if not installed_manifest.is_file():
        raise SystemExit(f"npm production package is not installed from the lockfile: {name} ({package_path})")
    installed_version = json.loads(installed_manifest.read_text(encoding="utf-8")).get("version")
    if installed_version != package.get("version"):
        raise SystemExit(f"npm package version differs from package-lock.json: {name} {installed_version} != {package.get('version')}")
    license_candidates = sorted(
        (path for pattern in ("LICENSE*", "COPYING*", "NOTICE*") for path in package_dir.glob(pattern) if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    if not license_candidates:
        raise SystemExit(f"npm package lacks a shipped license file: {name} {package.get('version', 'unknown')} ({package_path})")
    npm_license_text = "\n\n".join(path.read_text(encoding="utf-8", errors="replace") for path in license_candidates)
    npm_packages.append((name, package.get("version", "unknown"), package.get("license", "UNKNOWN"), npm_license_text))
npm_packages.sort(key=lambda item: (item[0].casefold(), item[1]))

macho_components = {}
for relative in macho_path.read_text(encoding="utf-8").splitlines():
    if relative == "poly-runtime":
        macho_components[relative] = "Poly launcher / PyInstaller bootloader"
        continue
    if relative.startswith("postgres/"):
        macho_components[relative] = "PostgreSQL"
        continue
    bundled_path = relative.removeprefix("_internal/")
    module_name = bundled_path.split("/", 1)[0].split(".", 1)[0]
    if (
        (relative.startswith("_internal/python") and relative.endswith(".dylib"))
        or bundled_path.startswith("lib-dynload/")
        or (relative.endswith(".so") and module_name in sys.stdlib_module_names)
    ):
        macho_components[relative] = "CPython"
        continue
    matching_distributions = sorted({
        name for name in package_distributions.get(module_name, [])
        if normalized_name(name) in production_distributions
    }, key=str.casefold)
    if relative.endswith(".so") and matching_distributions:
        macho_components[relative] = "Python production dependency: " + ", ".join(matching_distributions)
        continue
    explicit_license = native_root / f"{relative}.LICENSE"
    if not explicit_license.is_file():
        raise SystemExit(f"packaged native file lacks inventory: {relative}; add {explicit_license}")
    macho_components[relative] = f"Native library with explicit notice: {relative}.LICENSE"

files = []
for path in sorted((p for p in runtime_path.rglob("*") if p.is_file()), key=lambda p: p.relative_to(runtime_path).as_posix()):
    relative = path.relative_to(runtime_path).as_posix()
    if relative in macho_components:
        component = macho_components[relative]
    elif relative.startswith("postgres/"):
        component = "PostgreSQL"
    elif relative == "_internal/base_library.zip" or (relative.startswith("_internal/python") and relative.endswith(".dylib")):
        component = "CPython"
    else:
        component = "Frozen Python application or production dependency"
    files.append((f"poly-runtime/{relative}", component))

parts = [
    "# Third-Party Notices",
    "",
    "Generated deterministically by `packaging/macos/generate-notices.sh` from locked dependency files and the assembled macOS resources. Do not edit this release inventory by hand.",
    "",
    "## Application notice",
    "",
    "Poly is the top-level application. This document records third-party material distributed with it; it does not grant a license to Poly itself.",
    "",
    "## Rust production dependencies (including Tauri)",
    "",
    clean(rust_path.read_text(encoding="utf-8")),
    "",
    "## Frozen Python production environment",
]
for package in python_packages:
    parts += ["", f"### {package.get('Name', 'unknown')} {package.get('Version', 'unknown')}", "", f"Declared license: {package.get('License', 'UNKNOWN')}"]
    license_text = package.get("LicenseText") or package.get("License text")
    if not license_text:
        raise SystemExit(f"Python production package lacks license text: {package.get('Name', 'unknown')} {package.get('Version', 'unknown')}")
    parts += ["", fenced(license_text)]
parts += ["", "## npm production dependencies", ""]
for name, version, license_name, npm_license_text in npm_packages:
    parts += [f"### {name} {version}", "", f"Declared license: {license_name}", "", fenced(npm_license_text), ""]
parts += [
    "", "## CPython", "", fenced(cpython_path.read_text(encoding="utf-8", errors="replace")),
    "", "## PyInstaller bootloader", "", fenced(pyinstaller_path.read_text(encoding="utf-8", errors="replace")),
    "", "## PostgreSQL License", "", fenced(postgres_path.read_text(encoding="utf-8", errors="replace")),
    "", "## WebKit and macOS system libraries", "",
    "The Tauri application uses the WebKit framework supplied by macOS. WebKit and Apple system libraries are dynamically used system components and are not copied into this application bundle.",
    "", "## Packaged-file inventory", "",
    "Every assembled resource file is listed below. Native files other than the PyInstaller bootloader, CPython, and PostgreSQL require a matching license file under `packaging/macos/native-licenses`.",
    "", "| Packaged file | Inventory component |", "| --- | --- |",
]
parts += [f"| `{path}` | {component} |" for path, component in files]
if native_root.is_dir():
    for license_path in sorted(native_root.rglob("*.LICENSE"), key=lambda p: p.relative_to(native_root).as_posix().casefold()):
        parts += ["", f"### Native license: {license_path.relative_to(native_root).as_posix()}", "", fenced(license_path.read_text(encoding="utf-8", errors="replace"))]
pathlib.Path(output_path).write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8", newline="\n")
PY

if [[ "${MODE}" == "check" ]]; then
  cmp -s "${GENERATED}" "${OUTPUT}" || die "${OUTPUT} is stale; run packaging/macos/generate-notices.sh after assembling resources"
  printf 'Third-party notices match assembled resources and locked dependencies.\n'
else
  cp -- "${GENERATED}" "${OUTPUT}"
  printf 'Generated %s\n' "${OUTPUT}"
fi
