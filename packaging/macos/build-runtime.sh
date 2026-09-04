#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"

: "${POLY_TARGET_ARCH:?POLY_TARGET_ARCH must be set to arm64 or x86_64}"
: "${POLY_TARGET_TRIPLE:?POLY_TARGET_TRIPLE must be set to a macOS target triple}"

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
uv run pyinstaller --clean --noconfirm packaging/macos/poly-runtime.spec
