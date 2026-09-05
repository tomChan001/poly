#!/usr/bin/env bash
set -euo pipefail

readonly POSTGRES_VERSION="16.15"
readonly ARCHIVE="postgresql-${POSTGRES_VERSION}.tar.bz2"
readonly SOURCE_URL="https://ftp.postgresql.org/pub/source/v16.15/postgresql-16.15.tar.bz2"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly CHECKSUM_FILE="${SCRIPT_DIR}/postgres-SHA256SUMS"

die() {
  printf 'fetch-postgres: %s\n' "$*" >&2
  exit 1
}

AUDIT_ALLOWED_ROOT=""
FIND_BIN="find"
FILE_BIN="file"
OTOOL_BIN="otool"

is_macho() {
  local description
  if ! description="$("${FILE_BIN}" -b -- "$1")"; then
    die "file inspection failed for $1"
  fi
  case "${description}" in
    *Mach-O*) return 0 ;;
    *) return 1 ;;
  esac
}

otool_dependencies() {
  local owner="$1"
  local output
  if ! output="$("${OTOOL_BIN}" -L "${owner}")"; then
    die "otool -L failed for ${owner}"
  fi
  if ! printf '%s\n' "${output}" | awk 'NR > 1 { print $1 }'; then
    die "could not parse otool -L output for ${owner}"
  fi
}

otool_rpaths() {
  local owner="$1"
  local output
  if ! output="$("${OTOOL_BIN}" -l "${owner}")"; then
    die "otool -l failed for ${owner}"
  fi
  if ! printf '%s\n' "${output}" |
    awk '$1 == "cmd" && $2 == "LC_RPATH" { getline; getline; if ($1 == "path") print $2 }'; then
    die "could not parse LC_RPATH entries for ${owner}"
  fi
}

check_dependency() {
  local owner="$1"
  local dependency="$2"
  case "${dependency}" in
    @rpath/*|@loader_path/*|@executable_path/*|/usr/lib/*|/System/Library/*|/Library/Apple/System/Library/*|"${AUDIT_ALLOWED_ROOT}"/*)
      return 0
      ;;
    /opt/homebrew/*|/opt/local/*|/usr/local/*|/Users/*|/runner/*|/home/*|/private/var/folders/*|/Volumes/*)
      die "unsafe dependency in ${owner}: ${dependency}"
      ;;
    /*)
      die "unexpected absolute dependency in ${owner}: ${dependency}"
      ;;
    *)
      die "unexpected dependency or rpath in ${owner}: ${dependency}"
      ;;
  esac
}

audit_tree() {
  local tree="$1"
  local work_dir="$2"
  local candidate dependency rpath
  if ! "${FIND_BIN}" "${tree}" -type f -print0 >"${work_dir}/audit-files"; then
    die "find failed while auditing ${tree}"
  fi
  while IFS= read -r -d '' candidate; do
    is_macho "${candidate}" || continue
    if ! otool_dependencies "${candidate}" >"${work_dir}/dependencies"; then
      die "dependency inspection failed for ${candidate}"
    fi
    while IFS= read -r dependency; do
      [[ -n "${dependency}" ]] || continue
      check_dependency "${candidate}" "${dependency}"
    done <"${work_dir}/dependencies"
    if ! otool_rpaths "${candidate}" >"${work_dir}/rpaths"; then
      die "rpath inspection failed for ${candidate}"
    fi
    while IFS= read -r rpath; do
      [[ -n "${rpath}" ]] || continue
      check_dependency "${candidate} LC_RPATH" "${rpath}"
    done <"${work_dir}/rpaths"
  done <"${work_dir}/audit-files"
}

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
  FIND_BIN="${POLY_TEST_FIND:-find}"
  FILE_BIN="${POLY_TEST_FILE:-file}"
  OTOOL_BIN="${POLY_TEST_OTOOL:-otool}"
  for command_name in awk "${FILE_BIN}" "${FIND_BIN}" "${OTOOL_BIN}"; do
    command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
  done
  AUDIT_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-postgres-audit.XXXXXXXX")"
  cleanup_audit() {
    [[ -n "${AUDIT_TEMP:-}" && -d "${AUDIT_TEMP}" ]] && rm -rf -- "${AUDIT_TEMP}"
  }
  trap cleanup_audit EXIT INT TERM
  AUDIT_ALLOWED_ROOT="$2"
  audit_tree "$2" "${AUDIT_TEMP}"
  printf 'Mach-O dependency audit passed: %s\n' "$2"
  exit 0
fi

[[ "$#" -eq 0 ]] || die "usage: $0 [--check] | --audit-tree /absolute/tree"
[[ "$(uname -s)" == "Darwin" ]] || die "PostgreSQL distribution must be built natively on macOS"

readonly TARGET_TRIPLE="${POLY_TARGET_TRIPLE:-}"
case "${TARGET_TRIPLE}" in
  aarch64-apple-darwin)
    [[ "$(uname -m)" == "arm64" ]] || die "target ${TARGET_TRIPLE} requires a native arm64 runner"
    ;;
  x86_64-apple-darwin)
    [[ "$(uname -m)" == "x86_64" ]] || die "target ${TARGET_TRIPLE} requires a native x86_64 runner"
    ;;
  *)
    die "unsupported POLY_TARGET_TRIPLE '${TARGET_TRIPLE}'; expected aarch64-apple-darwin or x86_64-apple-darwin"
    ;;
esac

for command_name in awk curl "${FILE_BIN}" "${FIND_BIN}" install_name_tool make "${OTOOL_BIN}" shasum tar; do
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
AUDIT_ALLOWED_ROOT="${STAGED}"
(
  cd -- "${SOURCE_DIR}"
  ./configure \
    --prefix="${INSTALL_PREFIX}" \
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

if ! "${FIND_BIN}" "${INSTALL_PREFIX}/lib" -maxdepth 1 \( -type f -o -type l \) \
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

if ! "${FIND_BIN}" "${STAGED}" -type f -print0 >"${BUILD_TEMP}/staged-files"; then
  die "find failed while preparing staged Mach-O files"
fi
while IFS= read -r -d '' macho; do
  is_macho "${macho}" || continue
  if [[ "${macho}" == *.dylib || "${macho}" == *.dylib.* ]]; then
    install_name_tool -id "@rpath/$(basename -- "${macho}")" "${macho}"
  fi
  if ! otool_dependencies "${macho}" >"${BUILD_TEMP}/dependencies"; then
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
  if ! otool_rpaths "${macho}" >"${BUILD_TEMP}/rpaths"; then
    die "rpath inspection failed for ${macho}"
  fi
  while IFS= read -r rpath; do
    if [[ "${rpath}" == "${INSTALL_PREFIX}"/* ]]; then
      install_name_tool -delete_rpath "${rpath}" "${macho}"
    fi
  done <"${BUILD_TEMP}/rpaths"
done <"${BUILD_TEMP}/staged-files"

audit_tree "${STAGED}" "${BUILD_TEMP}"

mv -- "${STAGED}" "${DESTINATION}"
printf 'PostgreSQL %s assembled at %s\n' "${POSTGRES_VERSION}" "${DESTINATION}"
