#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'verify-runtime: %s\n' "$*" >&2
  exit 1
}

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
readonly BUNDLE_DIR="${REPO_ROOT}/dist/poly-runtime"
readonly RUNTIME_EXECUTABLE="${BUNDLE_DIR}/poly-runtime"
readonly RUNTIME_RESOURCE_ROOT="${BUNDLE_DIR}/_internal"
readonly POLY_REQUIRED_MACOS_TARGET="12.0"

: "${POLY_TARGET_ARCH:?POLY_TARGET_ARCH must be set to arm64 or x86_64}"
: "${POLY_TARGET_TRIPLE:?POLY_TARGET_TRIPLE must be set to a macOS target triple}"
[[ -z "${MACOSX_DEPLOYMENT_TARGET:-}" || "${MACOSX_DEPLOYMENT_TARGET}" == "${POLY_REQUIRED_MACOS_TARGET}" ]] ||
  die "MACOSX_DEPLOYMENT_TARGET must be ${POLY_REQUIRED_MACOS_TARGET}"
export MACOSX_DEPLOYMENT_TARGET="${POLY_REQUIRED_MACOS_TARGET}"

case "${POLY_TARGET_ARCH}:${POLY_TARGET_TRIPLE}" in
  arm64:aarch64-apple-darwin)
    EXPECTED_ARCH='arm64'
    ;;
  x86_64:x86_64-apple-darwin)
    EXPECTED_ARCH='x86_64'
    ;;
  *)
    die 'POLY_TARGET_ARCH and POLY_TARGET_TRIPLE must be a supported matching pair'
    ;;
esac

for command_name in awk file find lipo otool realpath; do
  command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
done

[[ -x "${RUNTIME_EXECUTABLE}" ]] ||
  die "built runtime executable is missing or not executable: ${RUNTIME_EXECUTABLE}"
for program in initdb postgres pg_isready psql createdb; do
  postgres_executable="${RUNTIME_RESOURCE_ROOT}/postgres/bin/${program}"
  [[ -x "${postgres_executable}" ]] ||
    die "required PostgreSQL program is missing or not executable: ${postgres_executable}"
done
for library in libpq.5.dylib plpgsql.dylib dict_snowball.dylib; do
  postgres_library="${RUNTIME_RESOURCE_ROOT}/postgres/lib/${library}"
  [[ -f "${postgres_library}" ]] ||
    die "required PostgreSQL library is missing: ${postgres_library}"
done

"${RUNTIME_EXECUTABLE}" --self-test
file "${RUNTIME_EXECUTABLE}"

VERIFY_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-runtime-verify.XXXXXXXX")"
cleanup() {
  [[ -n "${VERIFY_TEMP:-}" && -d "${VERIFY_TEMP}" ]] && rm -rf -- "${VERIFY_TEMP}"
}
trap cleanup EXIT INT TERM

MACHO_AUDIT_FIND_BIN="find"
MACHO_AUDIT_FILE_BIN="file"
MACHO_AUDIT_LIPO_BIN="lipo"
MACHO_AUDIT_OTOOL_BIN="otool"
MACHO_AUDIT_CODESIGN_BIN="codesign"
MACHO_AUDIT_CODESIGN=0
MACHO_AUDIT_BOUNDARY="${BUNDLE_DIR}"
MACHO_AUDIT_CONTEXTS_FILE="${VERIFY_TEMP}/executable-contexts"
MACHO_AUDIT_EXPECTED_ARCH="${EXPECTED_ARCH}"
MACHO_AUDIT_MAX_MIN_OS="${POLY_REQUIRED_MACOS_TARGET}"
# shellcheck source=packaging/macos/macho-audit.sh
source "${SCRIPT_DIR}/macho-audit.sh"

printf '%s\t%s\n' "${BUNDLE_DIR}" "${RUNTIME_EXECUTABLE}" >"${MACHO_AUDIT_CONTEXTS_FILE}"
printf '%s\t%s\n' "${RUNTIME_RESOURCE_ROOT}/postgres" "${RUNTIME_RESOURCE_ROOT}/postgres/bin/postgres" >>"${MACHO_AUDIT_CONTEXTS_FILE}"
macho_audit_tree "${BUNDLE_DIR}" "${VERIFY_TEMP}"
printf 'Runtime is thin %s and compatible with macOS %s: %s\n' \
  "${EXPECTED_ARCH}" "${POLY_REQUIRED_MACOS_TARGET}" "${BUNDLE_DIR}"
