#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
BUNDLE_DIR="${REPO_ROOT}/dist/poly-runtime"
RUNTIME_EXECUTABLE="${BUNDLE_DIR}/poly-runtime"

: "${POLY_TARGET_ARCH:?POLY_TARGET_ARCH must be set to arm64 or x86_64}"
: "${POLY_TARGET_TRIPLE:?POLY_TARGET_TRIPLE must be set to a macOS target triple}"

case "${POLY_TARGET_ARCH}:${POLY_TARGET_TRIPLE}" in
  arm64:aarch64-apple-darwin)
    EXPECTED_ARCH='arm64'
    ;;
  x86_64:x86_64-apple-darwin)
    EXPECTED_ARCH='x86_64'
    ;;
  *)
    printf '%s\n' \
      'POLY_TARGET_ARCH and POLY_TARGET_TRIPLE must be a supported matching pair' >&2
    exit 2
    ;;
esac

if [[ ! -x "${RUNTIME_EXECUTABLE}" ]]; then
  printf 'built runtime executable is missing or not executable: %s\n' \
    "${RUNTIME_EXECUTABLE}" >&2
  exit 1
fi

for program in initdb postgres pg_isready psql createdb; do
  postgres_executable="${BUNDLE_DIR}/postgres/bin/${program}"
  if [[ ! -x "${postgres_executable}" ]]; then
    printf 'required PostgreSQL program is missing or not executable: %s\n' \
      "${postgres_executable}" >&2
    exit 1
  fi
done

"${RUNTIME_EXECUTABLE}" --self-test
file "${RUNTIME_EXECUTABLE}"
find "${BUNDLE_DIR}" -type f -perm -111 -print0 | xargs -0 file

while IFS= read -r -d '' candidate; do
  description="$(file -b "${candidate}")"
  if [[ "${description}" != *Mach-O* ]]; then
    continue
  fi
  if [[ "${description}" != *"${EXPECTED_ARCH}"* ]]; then
    printf 'architecture mismatch for %s: expected %s, got %s\n' \
      "${candidate}" "${EXPECTED_ARCH}" "${description}" >&2
    exit 1
  fi

  dependencies="$(otool -L "${candidate}"; otool -l "${candidate}")"
  if grep -Eq \
    '^[[:space:]]+.*(/opt/homebrew/|/usr/local/(Cellar|opt)/|/opt/local/|/Users/|/Volumes/|/private/var/folders/)' \
    <<<"${dependencies}"; then
    printf 'forbidden non-system dependency path in %s\n' "${candidate}" >&2
    exit 1
  fi
done < <(
  find "${BUNDLE_DIR}" -type f \
    \( -perm -111 -o -name '*.dylib' -o -name '*.so' \) -print0
)

while IFS= read -r -d '' link; do
  if [[ ! -e "${link}" ]]; then
    printf 'broken symlink in packaged runtime: %s\n' "${link}" >&2
    exit 1
  fi
  target="$(readlink "${link}")"
  if [[ "${target}" = /* ]]; then
    printf 'absolute symlink escapes packaged runtime: %s\n' "${link}" >&2
    exit 1
  fi
  target_parent="$(cd -- "$(dirname -- "${link}")/$(dirname -- "${target}")" && pwd -P)"
  resolved_target="${target_parent}/$(basename -- "${target}")"
  case "${resolved_target}" in
    "${BUNDLE_DIR}"/*) ;;
    *)
      printf 'symlink escapes packaged runtime: %s\n' "${link}" >&2
      exit 1
      ;;
  esac
done < <(find "${BUNDLE_DIR}" -type l -print0)
