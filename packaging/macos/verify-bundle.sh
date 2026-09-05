#!/usr/bin/env bash
set -euo pipefail

readonly BUNDLE_ID="com.poly.desktop"

die() {
  printf 'verify-bundle: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--check" ]]; then
  [[ "$#" -eq 1 ]] || die "usage: $0 --check | /absolute/path/to/Poly.app"
  printf 'Bundle verifier static contract is available; signed launch verification requires macOS.\n'
  exit 0
fi

[[ "$#" -eq 1 ]] || die "usage: $0 --check | /absolute/path/to/Poly.app"
[[ "$(uname -s)" == "Darwin" ]] || die "bundle verification requires macOS"
case "$1" in
  /*.app) ;;
  *) die "pass exactly one absolute .app path" ;;
esac
[[ -d "$1" ]] || die "app bundle does not exist: $1"

for command_name in codesign file lsof open osascript otool ps realpath spctl xcrun; do
  command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
done

readonly APP_PATH="$(realpath "$1")"
[[ "${APP_PATH}" == /*.app ]] || die "resolved path is not an absolute .app bundle: ${APP_PATH}"
readonly APP_EXECUTABLE="${APP_PATH}/Contents/MacOS/Poly"
readonly RUNTIME_ROOT="${APP_PATH}/Contents/Resources/poly-runtime"
readonly RUNTIME_EXECUTABLE="${RUNTIME_ROOT}/poly-runtime"
[[ -x "${APP_EXECUTABLE}" ]] || die "missing exact application executable: ${APP_EXECUTABLE}"
[[ -x "${RUNTIME_EXECUTABLE}" ]] || die "missing exact runtime executable: ${RUNTIME_EXECUTABLE}"

codesign --verify --deep --strict --verbose=2 "${APP_PATH}"
spctl --assess --type execute --verbose=4 "${APP_PATH}"
xcrun stapler validate "${APP_PATH}"

is_macho() {
  file -b -- "$1" | grep -q 'Mach-O'
}

check_dependency() {
  local owner="$1"
  local dependency="$2"
  case "${dependency}" in
    @rpath/*|@loader_path/*|@executable_path/*|/usr/lib/*|/System/Library/*|/Library/Apple/System/Library/*|"${APP_PATH}"/*)
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
  codesign --verify --strict --verbose=2 "${candidate}"
  while IFS= read -r dependency; do
    check_dependency "${candidate}" "${dependency}"
  done < <(otool -L "${candidate}" | tail -n +2 | sed -E 's/^[[:space:]]*([^[:space:]]+).*/\1/')
  while IFS= read -r rpath; do
    check_dependency "${candidate} LC_RPATH" "${rpath}"
  done < <(otool -l "${candidate}" | awk '$1 == "cmd" && $2 == "LC_RPATH" { getline; getline; if ($1 == "path") print $2 }')
done < <(find "${APP_PATH}/Contents" -type f -print0)

pids_at_exact_executable() {
  local wanted="$1"
  local pid executable
  while read -r pid executable; do
    [[ "${executable}" == "${wanted}" ]] && printf '%s\n' "${pid}"
  done < <(ps -axo pid=,comm=)
}

pids_under_path() {
  local prefix="$1"
  local pid executable
  while read -r pid executable; do
    [[ "${executable}" == "${prefix}"* ]] && printf '%s\n' "${pid}"
  done < <(ps -axo pid=,comm=)
}

COLLECTED_PIDS=()
collect_pids() {
  local selector="$1"
  local path="$2"
  local pid
  COLLECTED_PIDS=()
  while IFS= read -r pid; do
    [[ -n "${pid}" ]] && COLLECTED_PIDS[${#COLLECTED_PIDS[@]}]="${pid}"
  done < <("${selector}" "${path}")
}

collect_pids pids_at_exact_executable "${APP_EXECUTABLE}"
existing_app=("${COLLECTED_PIDS[@]}")
collect_pids pids_under_path "${RUNTIME_ROOT}/"
existing_runtime=("${COLLECTED_PIDS[@]}")
if [[ "${#existing_app[@]}" -ne 0 || "${#existing_runtime[@]}" -ne 0 ]]; then
  die "refusing to mix verification with existing bundle processes"
fi

LAUNCHED=0
quit_launched_app() {
  if [[ "${LAUNCHED}" -eq 1 ]]; then
    osascript -e 'tell application id "com.poly.desktop" to quit' >/dev/null 2>&1 || true
  fi
}
trap quit_launched_app EXIT INT TERM

open -n "${APP_PATH}"
LAUNCHED=1

declare -a OWNED_PIDS=()
ready=0
for ((attempt = 0; attempt < 60; attempt++)); do
  collect_pids pids_at_exact_executable "${APP_EXECUTABLE}"
  app_pids=("${COLLECTED_PIDS[@]}")
  collect_pids pids_under_path "${RUNTIME_ROOT}/"
  runtime_pids=("${COLLECTED_PIDS[@]}")
  for pid in "${app_pids[@]}" "${runtime_pids[@]}"; do
    [[ -n "${pid}" ]] || continue
    if [[ ! " ${OWNED_PIDS[*]:-} " =~ " ${pid} " ]]; then
      OWNED_PIDS+=("${pid}")
    fi
  done
  for pid in "${runtime_pids[@]}"; do
    if lsof -nP -a -p "${pid}" -iTCP -sTCP:LISTEN 2>/dev/null | grep -q 'TCP 127.0.0.1:'; then
      ready=1
      break 2
    fi
  done
  sleep 1
done
[[ "${ready}" -eq 1 ]] || die "application did not expose an owned loopback listener within 60 seconds"

osascript -e 'tell application id "com.poly.desktop" to quit'
LAUNCHED=0
for ((attempt = 0; attempt < 30; attempt++)); do
  collect_pids pids_at_exact_executable "${APP_EXECUTABLE}"
  remaining_app=("${COLLECTED_PIDS[@]}")
  collect_pids pids_under_path "${RUNTIME_ROOT}/"
  remaining_runtime=("${COLLECTED_PIDS[@]}")
  if [[ "${#remaining_app[@]}" -eq 0 && "${#remaining_runtime[@]}" -eq 0 ]]; then
    break
  fi
  sleep 1
done

collect_pids pids_at_exact_executable "${APP_EXECUTABLE}"
remaining_app=("${COLLECTED_PIDS[@]}")
collect_pids pids_under_path "${RUNTIME_ROOT}/"
remaining_runtime=("${COLLECTED_PIDS[@]}")
[[ "${#remaining_app[@]}" -eq 0 ]] || die "application process remained after Apple Events quit: ${remaining_app[*]}"
[[ "${#remaining_runtime[@]}" -eq 0 ]] || die "process remained under exact runtime path: ${remaining_runtime[*]}"
for pid in "${OWNED_PIDS[@]}"; do
  if lsof -nP -a -p "${pid}" -iTCP -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    die "listener remains attributable to captured owned PID ${pid}"
  fi
done

printf 'Signed, notarized bundle passed path-scoped launch and shutdown verification: %s\n' "${APP_PATH}"
