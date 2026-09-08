#!/usr/bin/env bash
set -euo pipefail

readonly POSTGRES_VERSION="16.15"
readonly ARCHIVE="postgresql-${POSTGRES_VERSION}.tar.bz2"
readonly SOURCE_URL="https://ftp.postgresql.org/pub/source/v16.15/postgresql-16.15.tar.bz2"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly CHECKSUM_FILE="${SCRIPT_DIR}/postgres-SHA256SUMS"
readonly POLY_REQUIRED_MACOS_TARGET="12.0"

die() {
  printf 'fetch-postgres: %s\n' "$*" >&2
  exit 1
}

MACHO_AUDIT_FIND_BIN="find"
MACHO_AUDIT_FILE_BIN="file"
MACHO_AUDIT_LIPO_BIN="lipo"
MACHO_AUDIT_OTOOL_BIN="otool"
MACHO_AUDIT_CODESIGN_BIN="codesign"
MACHO_AUDIT_CODESIGN=0
MACHO_AUDIT_BOUNDARY=""
MACHO_AUDIT_CONTEXTS_FILE=""
MACHO_AUDIT_EXPECTED_ARCH=""
MACHO_AUDIT_MAX_MIN_OS=""
# shellcheck source=packaging/macos/macho-audit.sh
source "${SCRIPT_DIR}/macho-audit.sh"

if [[ "${1:-}" == "--check" ]]; then
  [[ "$#" -eq 1 ]] || die "--check accepts no other arguments"
  [[ "$(cat "${CHECKSUM_FILE}")" == "c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed  ${ARCHIVE}" ]] ||
    die "${CHECKSUM_FILE} does not contain the official PostgreSQL ${POSTGRES_VERSION} checksum"
  printf 'PostgreSQL source pin is valid.\n'
  exit 0
fi

if [[ "${1:-}" == "--audit-tree" ]]; then
  [[ "$#" -eq 2 && -d "$2" ]] || die "usage: $0 --audit-tree /absolute/tree"
  # Fault-injection overrides are accepted only by this non-building test mode.
  MACHO_AUDIT_FIND_BIN="${POLY_TEST_FIND:-find}"
  MACHO_AUDIT_FILE_BIN="${POLY_TEST_FILE:-file}"
  MACHO_AUDIT_LIPO_BIN="${POLY_TEST_LIPO:-lipo}"
  MACHO_AUDIT_OTOOL_BIN="${POLY_TEST_OTOOL:-otool}"
  MACHO_AUDIT_EXPECTED_ARCH="${POLY_TEST_EXPECTED_ARCH:-}"
  MACHO_AUDIT_MAX_MIN_OS="${POLY_TEST_MAX_MIN_OS:-}"
  audit_commands=(awk realpath "${MACHO_AUDIT_FILE_BIN}" "${MACHO_AUDIT_FIND_BIN}" "${MACHO_AUDIT_OTOOL_BIN}")
  [[ -z "${MACHO_AUDIT_EXPECTED_ARCH}" ]] || audit_commands+=("${MACHO_AUDIT_LIPO_BIN}")
  for command_name in "${audit_commands[@]}"; do
    command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
  done
  AUDIT_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-postgres-audit.XXXXXXXX")"
  cleanup_audit() {
    [[ -n "${AUDIT_TEMP:-}" && -d "${AUDIT_TEMP}" ]] && rm -rf -- "${AUDIT_TEMP}"
  }
  trap cleanup_audit EXIT INT TERM
  MACHO_AUDIT_BOUNDARY="${POLY_TEST_AUDIT_BOUNDARY:-$2}"
  MACHO_AUDIT_CONTEXTS_FILE="${AUDIT_TEMP}/executable-contexts"
  : >"${MACHO_AUDIT_CONTEXTS_FILE}"
  if [[ -n "${POLY_TEST_MAIN_EXECUTABLE:-}" ]]; then
    printf '%s\t%s\n' "$2" "${POLY_TEST_MAIN_EXECUTABLE}" >"${MACHO_AUDIT_CONTEXTS_FILE}"
  fi
  macho_audit_tree "$2" "${AUDIT_TEMP}"
  printf 'Mach-O dependency audit passed: %s\n' "$2"
  exit 0
fi

[[ "$#" -eq 0 ]] || die "usage: $0 [--check] | --audit-tree /absolute/tree"
[[ "$(uname -s)" == "Darwin" ]] || die "PostgreSQL distribution must be built natively on macOS"
[[ -z "${MACOSX_DEPLOYMENT_TARGET:-}" || "${MACOSX_DEPLOYMENT_TARGET}" == "${POLY_REQUIRED_MACOS_TARGET}" ]] ||
  die "MACOSX_DEPLOYMENT_TARGET must be ${POLY_REQUIRED_MACOS_TARGET}"
export MACOSX_DEPLOYMENT_TARGET="${POLY_REQUIRED_MACOS_TARGET}"
export CFLAGS="${CFLAGS:+${CFLAGS} }-mmacosx-version-min=${POLY_REQUIRED_MACOS_TARGET}"
export CXXFLAGS="${CXXFLAGS:+${CXXFLAGS} }-mmacosx-version-min=${POLY_REQUIRED_MACOS_TARGET}"
export LDFLAGS="${LDFLAGS:+${LDFLAGS} }-mmacosx-version-min=${POLY_REQUIRED_MACOS_TARGET}"

readonly TARGET_TRIPLE="${POLY_TARGET_TRIPLE:-}"
case "${TARGET_TRIPLE}" in
  aarch64-apple-darwin)
    [[ "$(uname -m)" == "arm64" ]] || die "target ${TARGET_TRIPLE} requires a native arm64 runner"
    MACHO_AUDIT_EXPECTED_ARCH="arm64"
    ;;
  x86_64-apple-darwin)
    [[ "$(uname -m)" == "x86_64" ]] || die "target ${TARGET_TRIPLE} requires a native x86_64 runner"
    MACHO_AUDIT_EXPECTED_ARCH="x86_64"
    ;;
  *)
    die "unsupported POLY_TARGET_TRIPLE '${TARGET_TRIPLE}'; expected aarch64-apple-darwin or x86_64-apple-darwin"
    ;;
esac
MACHO_AUDIT_MAX_MIN_OS="${POLY_REQUIRED_MACOS_TARGET}"

for command_name in awk curl file find install_name_tool lipo make otool realpath shasum tar; do
  command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
done

readonly DESTINATION_PARENT="${SCRIPT_DIR}/postgres"
readonly DESTINATION="${DESTINATION_PARENT}/${TARGET_TRIPLE}"
[[ ! -e "${DESTINATION}" ]] || die "destination already exists; remove this exact architecture directory before rebuilding: ${DESTINATION}"
mkdir -p "${DESTINATION_PARENT}"

BUILD_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-postgres.XXXXXXXX")"
cleanup() {
  [[ -n "${BUILD_TEMP:-}" && -d "${BUILD_TEMP}" ]] && rm -rf -- "${BUILD_TEMP}"
}
trap cleanup EXIT INT TERM

curl --fail --location --proto '=https' --tlsv1.2 --output "${BUILD_TEMP}/${ARCHIVE}" "${SOURCE_URL}"
cp -- "${CHECKSUM_FILE}" "${BUILD_TEMP}/SHA256SUMS"
(
  cd -- "${BUILD_TEMP}"
  shasum -a 256 -c SHA256SUMS
)
tar -xjf "${BUILD_TEMP}/${ARCHIVE}" -C "${BUILD_TEMP}"

readonly SOURCE_DIR="${BUILD_TEMP}/postgresql-${POSTGRES_VERSION}"
readonly INSTALL_PREFIX="${BUILD_TEMP}/install"
readonly STAGED="${BUILD_TEMP}/staged"
(
  cd -- "${SOURCE_DIR}"
  ./configure \
    --prefix="${INSTALL_PREFIX}" \
    --without-icu \
    --without-readline \
    --without-zlib \
    --disable-nls
  make -j"$(sysctl -n hw.logicalcpu)" world-bin
  make install-world-bin
)

mkdir -p "${STAGED}/bin" "${STAGED}/lib" "${STAGED}/share"
for program in initdb postgres pg_isready psql createdb; do
  [[ -x "${INSTALL_PREFIX}/bin/${program}" ]] || die "expected installed program is missing: ${program}"
  cp -- "${INSTALL_PREFIX}/bin/${program}" "${STAGED}/bin/${program}"
done

if ! find "${INSTALL_PREFIX}/lib" -maxdepth 1 \( -type f -o -type l \) \
  -name 'libpq*.dylib*' -print0 >"${BUILD_TEMP}/libpq-files"; then
  die "find failed while locating libpq dylibs"
fi
while IFS= read -r -d '' library; do
  cp -P -- "${library}" "${STAGED}/lib/"
done <"${BUILD_TEMP}/libpq-files"
[[ -f "${INSTALL_PREFIX}/lib/postgresql/plpgsql.so" ]] || die "required PL/pgSQL extension library is missing"
mkdir -p "${STAGED}/lib/postgresql"
cp -- "${INSTALL_PREFIX}/lib/postgresql/plpgsql.so" "${STAGED}/lib/postgresql/plpgsql.so"
cp -R -- "${INSTALL_PREFIX}/share/." "${STAGED}/share/"
cp -- "${SOURCE_DIR}/COPYRIGHT" "${STAGED}/COPYRIGHT"

if ! find "${STAGED}" -type f -print0 >"${BUILD_TEMP}/staged-files"; then
  die "find failed while preparing staged Mach-O files"
fi
while IFS= read -r -d '' macho; do
  macho_is_macho "${macho}" || continue
  if [[ "${macho}" == *.dylib || "${macho}" == *.dylib.* ]]; then
    install_name_tool -id "@rpath/$(basename -- "${macho}")" "${macho}"
  fi
  if ! macho_otool_dependencies "${macho}" >"${BUILD_TEMP}/dependencies"; then
    die "dependency inspection failed for ${macho}"
  fi
  while IFS= read -r dependency; do
    [[ "${dependency}" == "${INSTALL_PREFIX}/lib/"* ]] || continue
    dependency_name="$(basename -- "${dependency}")"
    if [[ "${macho}" == "${STAGED}/bin/"* ]]; then
      replacement="@loader_path/../lib/${dependency_name}"
    elif [[ "${macho}" == "${STAGED}/lib/postgresql/"* ]]; then
      replacement="@loader_path/../${dependency_name}"
    else
      replacement="@loader_path/${dependency_name}"
    fi
    install_name_tool -change "${dependency}" "${replacement}" "${macho}"
  done <"${BUILD_TEMP}/dependencies"
  if ! macho_otool_rpaths "${macho}" >"${BUILD_TEMP}/rpaths"; then
    die "rpath inspection failed for ${macho}"
  fi
  while IFS= read -r rpath; do
    if [[ "${rpath}" == "${INSTALL_PREFIX}"/* ]]; then
      install_name_tool -delete_rpath "${rpath}" "${macho}"
    fi
  done <"${BUILD_TEMP}/rpaths"
done <"${BUILD_TEMP}/staged-files"

MACHO_AUDIT_BOUNDARY="${STAGED}"
MACHO_AUDIT_CONTEXTS_FILE="${BUILD_TEMP}/executable-contexts"
printf '%s\t%s\n' "${STAGED}" "${STAGED}/bin/postgres" >"${MACHO_AUDIT_CONTEXTS_FILE}"
macho_audit_tree "${STAGED}" "${BUILD_TEMP}"

mv -- "${STAGED}" "${DESTINATION}"
printf 'PostgreSQL %s assembled at %s\n' "${POSTGRES_VERSION}" "${DESTINATION}"
