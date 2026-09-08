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

CHECK=0
ASSEMBLED=0
for argument in "$@"; do
  case "${argument}" in
    --check|--static-check) CHECK=1 ;;
    --assembled) ASSEMBLED=1 ;;
    *) die "usage: $0 [--check|--static-check] [--assembled]" ;;
  esac
done
[[ "$#" -le 2 ]] || die "usage: $0 [--check|--static-check] [--assembled]"

cd -- "${REPO_ROOT}"
for command_name in cargo uv; do
  command -v "${command_name}" >/dev/null 2>&1 ||
    die "required command not found: ${command_name}; install the locked build toolchain"
done

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/poly-notices.XXXXXXXX")"
cleanup() {
  [[ -n "${WORK_DIR:-}" && -d "${WORK_DIR}" ]] && rm -rf -- "${WORK_DIR}"
}
trap cleanup EXIT INT TERM

for target in aarch64-apple-darwin x86_64-apple-darwin; do
  if ! cargo metadata --locked --format-version 1 --filter-platform "${target}" \
    --manifest-path "${REPO_ROOT}/src-tauri/Cargo.toml" \
    >"${WORK_DIR}/cargo-${target}.json"; then
    die "cargo metadata --locked failed for ${target}"
  fi
done

if ! uv export --frozen --no-dev --no-emit-project \
  --output-file "${WORK_DIR}/python-production.txt" >/dev/null; then
  die "uv export --frozen --no-dev failed; synchronize the committed uv.lock"
fi

readonly CPYTHON_LICENSE="${SCRIPT_DIR}/CPython-LICENSE.txt"
[[ -f "${CPYTHON_LICENSE}" ]] ||
  die "pinned macOS CPython license is missing: ${CPYTHON_LICENSE}"

if ! PYINSTALLER_LICENSE="$(uv run --frozen python - <<'PY'
from pathlib import Path
import importlib.metadata

distribution = importlib.metadata.distribution("PyInstaller")
candidates = [Path(distribution.locate_file(entry)) for entry in distribution.files or ()
              if str(entry).replace("\\", "/").endswith("/licenses/COPYING.txt")]
for candidate in candidates:
    if candidate.is_file():
        print(candidate)
        break
else:
    raise SystemExit("PyInstaller COPYING.txt is missing")
PY
)"; then
  die "could not locate the PyInstaller bootloader license in the frozen environment"
fi

GENERATOR_ARGS=(
  --rust-metadata "${WORK_DIR}/cargo-aarch64-apple-darwin.json"
  --rust-metadata "${WORK_DIR}/cargo-x86_64-apple-darwin.json"
  --python-requirements "${WORK_DIR}/python-production.txt"
  --uv-lock "${REPO_ROOT}/uv.lock"
  --npm-lock "${REPO_ROOT}/frontend/package-lock.json"
  --cpython-license "${CPYTHON_LICENSE}"
  --pyinstaller-license "${PYINSTALLER_LICENSE}"
  --output "${WORK_DIR}/THIRD_PARTY_NOTICES.md"
)

# CI should install cargo-license and pip-licenses.  When present, their
# independently resolved reports are compared with the lockfile inventory.
if cargo license --version >/dev/null 2>&1; then
  if ! cargo license --json --avoid-dev-deps \
    --manifest-path "${REPO_ROOT}/src-tauri/Cargo.toml" \
    >"${WORK_DIR}/cargo-license.json"; then
    die "cargo license failed; fix its inventory before producing notices"
  fi
  GENERATOR_ARGS+=(--cargo-license "${WORK_DIR}/cargo-license.json")
fi
if uv run --frozen pip-licenses --version >/dev/null 2>&1; then
  if ! uv run --frozen pip-licenses --format=json --with-urls \
    >"${WORK_DIR}/pip-licenses.json"; then
    die "pip-licenses failed; fix its inventory before producing notices"
  fi
  GENERATOR_ARGS+=(--pip-licenses "${WORK_DIR}/pip-licenses.json")
fi

if [[ "${ASSEMBLED}" -eq 1 ]]; then
  command -v file >/dev/null 2>&1 || die "required command not found: file"
  readonly RUNTIME="${REPO_ROOT}/src-tauri/resources/poly-runtime"
  [[ -x "${RUNTIME}/poly-runtime" ]] ||
    die "assembled runtime launcher is missing: ${RUNTIME}/poly-runtime"
  if [[ ! -f "${RUNTIME}/postgres/COPYRIGHT" && ! -f "${RUNTIME}/_internal/postgres/COPYRIGHT" ]]; then
    die "assembled PostgreSQL COPYRIGHT is missing from root or _internal layout"
  fi
  if ! find "${RUNTIME}" -type f -print >"${WORK_DIR}/runtime-files.txt"; then
    die "find failed while inventorying the assembled runtime"
  fi
  : >"${WORK_DIR}/macho-inventory.txt"
  while IFS= read -r packaged_file; do
    [[ -n "${packaged_file}" ]] || continue
    if ! description="$(file -b -- "${packaged_file}")"; then
      die "file inspection failed for ${packaged_file}"
    fi
    case "${description}" in
      *Mach-O*)
        relative="${packaged_file#"${RUNTIME}/"}"
        printf '%s\n' "${relative}" >>"${WORK_DIR}/macho-inventory.txt"
        ;;
    esac
  done <"${WORK_DIR}/runtime-files.txt"
  GENERATOR_ARGS+=(
    --runtime "${RUNTIME}"
    --macho-inventory "${WORK_DIR}/macho-inventory.txt"
    --native-license-root "${REPO_ROOT}/packaging/macos/native-licenses"
  )
fi

if ! uv run --frozen python "${SCRIPT_DIR}/notice_generator.py" "${GENERATOR_ARGS[@]}"; then
  die "notice generation failed; every locked and packaged component needs license/source metadata"
fi

if [[ "${CHECK}" -eq 1 ]]; then
  [[ -f "${OUTPUT}" ]] || die "generated notice is not committed: ${OUTPUT}"
  cmp -s "${WORK_DIR}/THIRD_PARTY_NOTICES.md" "${OUTPUT}" ||
    die "${OUTPUT} is stale; run packaging/macos/generate-notices.sh and commit the result"
  printf 'Third-party notices match committed lockfiles and available authoritative inventories.\n'
else
  cp -- "${WORK_DIR}/THIRD_PARTY_NOTICES.md" "${OUTPUT}"
  printf 'Generated %s\n' "${OUTPUT}"
fi
