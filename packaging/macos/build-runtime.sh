#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"

: "${POLY_TARGET_ARCH:?POLY_TARGET_ARCH must be set to arm64 or x86_64}"
: "${POLY_TARGET_TRIPLE:?POLY_TARGET_TRIPLE must be set to a macOS target triple}"
EXPECTED_PARENT_TEAM_ID="${POLY_EXPECTED_PARENT_TEAM_ID:-ADHOC_VALIDATION_ONLY}"
[[ "${EXPECTED_PARENT_TEAM_ID}" == 'ADHOC_VALIDATION_ONLY' || "${EXPECTED_PARENT_TEAM_ID}" =~ ^[A-Z0-9]{10}$ ]] || {
  printf '%s\n' 'POLY_EXPECTED_PARENT_TEAM_ID must be a 10-character Team ID' >&2
  exit 2
}

case "${POLY_TARGET_ARCH}:${POLY_TARGET_TRIPLE}" in
  arm64:aarch64-apple-darwin | x86_64:x86_64-apple-darwin) ;;
  *)
    printf '%s\n' \
      'POLY_TARGET_ARCH and POLY_TARGET_TRIPLE must be a supported matching pair' >&2
    exit 2
    ;;
esac

(
  cd -- "${REPO_ROOT}/frontend"
  npm ci
  npm run build
)

cd -- "${REPO_ROOT}"
uv sync --frozen
IDENTITY_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-parent-identity.XXXXXXXX")"
cleanup() {
  rm -rf -- "${IDENTITY_TEMP}"
}
trap cleanup EXIT INT TERM
printf '%s\n' "${EXPECTED_PARENT_TEAM_ID}" >"${IDENTITY_TEMP}/expected-parent-team-id"
export POLY_PARENT_IDENTITY_FILE="${IDENTITY_TEMP}/expected-parent-team-id"
uv run pyinstaller --clean --noconfirm packaging/macos/poly-runtime.spec
