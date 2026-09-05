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

if [[ "${1:-}" == "--check" ]]; then
  [[ "$#" -eq 1 ]] || die "--check accepts no other arguments"
  [[ "$(cat "${CHECKSUM_FILE}")" == "c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed  ${ARCHIVE}" ]] ||
    die "${CHECKSUM_FILE} does not contain the official PostgreSQL ${POSTGRES_VERSION} checksum"
  printf 'PostgreSQL source pin is valid.\n'
  exit 0
fi

[[ "$#" -eq 0 ]] || die "usage: $0 [--check]"
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

for command_name in curl make otool file install_name_tool shasum tar; do
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

while IFS= read -r -d '' library; do
  cp -P -- "${library}" "${STAGED}/lib/"
done < <(find "${INSTALL_PREFIX}/lib" -maxdepth 1 \( -type f -o -type l \) -name 'libpq*.dylib*' -print0)
[[ -f "${INSTALL_PREFIX}/lib/postgresql/plpgsql.so" ]] || die "required PL/pgSQL extension library is missing"
mkdir -p "${STAGED}/lib/postgresql"
cp -- "${INSTALL_PREFIX}/lib/postgresql/plpgsql.so" "${STAGED}/lib/postgresql/plpgsql.so"
cp -R -- "${INSTALL_PREFIX}/share/." "${STAGED}/share/"
cp -- "${SOURCE_DIR}/COPYRIGHT" "${STAGED}/COPYRIGHT"

is_macho() {
  file -b -- "$1" | grep -q 'Mach-O'
}

while IFS= read -r -d '' macho; do
  is_macho "${macho}" || continue
  if [[ "${macho}" == *.dylib || "${macho}" == *.dylib.* ]]; then
    install_name_tool -id "@rpath/$(basename -- "${macho}")" "${macho}"
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
  done < <(otool -L "${macho}" | tail -n +2 | sed -E 's/^[[:space:]]*([^[:space:]]+).*/\1/')
  while IFS= read -r rpath; do
    if [[ "${rpath}" == "${INSTALL_PREFIX}"/* ]]; then
      install_name_tool -delete_rpath "${rpath}" "${macho}"
    fi
  done < <(otool -l "${macho}" | awk '$1 == "cmd" && $2 == "LC_RPATH" { getline; getline; if ($1 == "path") print $2 }')
done < <(find "${STAGED}" -type f -print0)

check_dependency() {
  local owner="$1"
  local dependency="$2"
  case "${dependency}" in
    @rpath/*|@loader_path/*|@executable_path/*|/usr/lib/*|/System/Library/*|/Library/Apple/System/Library/*|"${STAGED}"/*)
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

while IFS= read -r -d '' candidate; do
  is_macho "${candidate}" || continue
  while IFS= read -r dependency; do
    check_dependency "${candidate}" "${dependency}"
  done < <(otool -L "${candidate}" | tail -n +2 | sed -E 's/^[[:space:]]*([^[:space:]]+).*/\1/')
  while IFS= read -r rpath; do
    check_dependency "${candidate} LC_RPATH" "${rpath}"
  done < <(otool -l "${candidate}" | awk '$1 == "cmd" && $2 == "LC_RPATH" { getline; getline; if ($1 == "path") print $2 }')
done < <(find "${STAGED}" -type f -print0)

mv -- "${STAGED}" "${DESTINATION}"
printf 'PostgreSQL %s assembled at %s\n' "${POSTGRES_VERSION}" "${DESTINATION}"
