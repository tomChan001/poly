#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'verify-bundle: %s\n' "$*" >&2
  exit 1
}

PS_BIN="ps"
LSOF_BIN="lsof"
VERIFY_TEMP=""
PROCESS_EXECUTABLE=""

cleanup_temp() {
  [[ -n "${VERIFY_TEMP:-}" && -d "${VERIFY_TEMP}" ]] && rm -rf -- "${VERIFY_TEMP}"
}

process_executable() {
  local pid="$1"
  local lsof_output ps_output record path
  PROCESS_EXECUTABLE=""
  if ! lsof_output="$("${LSOF_BIN}" -a -p "${pid}" -d txt -Fn 2>"${VERIFY_TEMP}/lsof-txt-error")"; then
    if ! ps_output="$("${PS_BIN}" -ww -p "${pid}" -o pid=)"; then
      die "process enumeration failed while checking PID ${pid} after lsof failure"
    fi
    [[ -z "${ps_output//[[:space:]]/}" ]] && return 1
    if [[ -s "${VERIFY_TEMP}/lsof-txt-error" ]]; then
      die "exact executable enumeration failed for live PID ${pid}: $(<"${VERIFY_TEMP}/lsof-txt-error")"
    fi
    die "exact executable enumeration failed for live PID ${pid}"
  fi
  while IFS= read -r record; do
    case "${record}" in
      n*)
        path="${record#n}"
        [[ -n "${path}" ]] || continue
        if ! PROCESS_EXECUTABLE="$(realpath "${path}")"; then
          die "could not canonicalize executable for PID ${pid}: ${path}"
        fi
        return 0
        ;;
    esac
  done <<<"${lsof_output}"
  die "exact executable enumeration returned no text path for live PID ${pid}"
}

enumerate_process_paths() {
  local candidate_prefix="$1"
  local output_file="$2"
  local record pid arguments runtime_directory
  : >"${output_file}"
  : >"${VERIFY_TEMP}/candidate-pids"
  if ! "${PS_BIN}" -ww -axo pid=,args= >"${VERIFY_TEMP}/ps-processes"; then
    die "process enumeration failed: ps -ww could not list processes"
  fi
  while IFS= read -r record; do
    [[ "${record}" =~ ^[[:space:]]*([0-9]+)[[:space:]]+(.*)$ ]] || continue
    pid="${BASH_REMATCH[1]}"
    arguments="${BASH_REMATCH[2]}"
    case "${arguments}" in
      *"${candidate_prefix}"*) ;;
      *) continue ;;
    esac
    printf '%s\n' "${pid}" >>"${VERIFY_TEMP}/candidate-pids"
  done <"${VERIFY_TEMP}/ps-processes"

  # A runtime such as postgres may replace argv after launch.  A recursive,
  # path-scoped lsof query finds those candidates without searching by name or
  # inspecting unrelated system paths; exact executable identity is checked below.
  case "${candidate_prefix}" in
    */)
      runtime_directory="${candidate_prefix%/}"
      : >"${VERIFY_TEMP}/path-lsof-error"
      if "${LSOF_BIN}" -nP +D "${runtime_directory}" -Fp \
        >"${VERIFY_TEMP}/path-lsof" 2>"${VERIFY_TEMP}/path-lsof-error"; then
        [[ ! -s "${VERIFY_TEMP}/path-lsof-error" ]] ||
          die "path-scoped process enumeration was incomplete for ${runtime_directory}: $(<"${VERIFY_TEMP}/path-lsof-error")"
        while IFS= read -r record; do
          case "${record}" in
            p[0-9]*)
              pid="${record#p}"
              if ! grep -qx "${pid}" "${VERIFY_TEMP}/candidate-pids"; then
                printf '%s\n' "${pid}" >>"${VERIFY_TEMP}/candidate-pids"
              fi
              ;;
          esac
        done <"${VERIFY_TEMP}/path-lsof"
      elif [[ -s "${VERIFY_TEMP}/path-lsof-error" ]]; then
        die "path-scoped process enumeration failed for ${runtime_directory}: $(<"${VERIFY_TEMP}/path-lsof-error")"
      fi
      ;;
  esac

  while IFS= read -r pid; do
    [[ -n "${pid}" ]] || continue
    if process_executable "${pid}"; then
      printf '%s\t%s\n' "${pid}" "${PROCESS_EXECUTABLE}" >>"${output_file}"
    fi
  done <"${VERIFY_TEMP}/candidate-pids"
}

pids_at_exact_executable() {
  local wanted="$1"
  local scan_file="${VERIFY_TEMP}/exact-processes"
  local pid executable
  enumerate_process_paths "${wanted}" "${scan_file}"
  while IFS=$'\t' read -r pid executable; do
    [[ "${executable}" == "${wanted}" ]] && printf '%s\n' "${pid}"
  done <"${scan_file}"
}

pids_under_path() {
  local prefix="$1"
  local scan_file="${VERIFY_TEMP}/runtime-processes"
  local pid executable
  enumerate_process_paths "${prefix}" "${scan_file}"
  while IFS=$'\t' read -r pid executable; do
    [[ "${executable}" == "${prefix}"* ]] && printf '%s\n' "${pid}"
  done <"${scan_file}"
}

if [[ "${1:-}" == "--check" ]]; then
  [[ "$#" -eq 1 ]] || die "usage: $0 --check | --enumerate-runtime /absolute/runtime/ | /absolute/path/to/Poly.app"
  printf 'Bundle verifier static contract is available; signed launch verification requires macOS.\n'
  exit 0
fi

if [[ "${1:-}" == "--enumerate-runtime" ]]; then
  [[ "$#" -eq 2 && "$2" == /*/ ]] || die "usage: $0 --enumerate-runtime /absolute/runtime/"
  # Fault-injection overrides are accepted only by this non-launching test mode.
  PS_BIN="${POLY_TEST_PS:-ps}"
  LSOF_BIN="${POLY_TEST_LSOF:-lsof}"
  VERIFY_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-bundle-processes.XXXXXXXX")"
  trap cleanup_temp EXIT INT TERM
  pids_under_path "$2"
  exit 0
fi

[[ "$#" -eq 1 ]] || die "usage: $0 --check | /absolute/path/to/Poly.app"
[[ "$(uname -s)" == "Darwin" ]] || die "bundle verification requires macOS"
case "$1" in
  /*.app) ;;
  *) die "pass exactly one absolute .app path" ;;
esac
[[ -d "$1" ]] || die "app bundle does not exist: $1"

for command_name in codesign file find "${LSOF_BIN}" open osascript otool "${PS_BIN}" realpath spctl xcrun; do
  command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
done

readonly APP_PATH="$(realpath "$1")"
[[ "${APP_PATH}" == /*.app ]] || die "resolved path is not an absolute .app bundle: ${APP_PATH}"
readonly APP_EXECUTABLE="${APP_PATH}/Contents/MacOS/Poly"
readonly RUNTIME_ROOT="${APP_PATH}/Contents/Resources/poly-runtime"
readonly RUNTIME_EXECUTABLE="${RUNTIME_ROOT}/poly-runtime"
[[ -x "${APP_EXECUTABLE}" ]] || die "missing exact application executable: ${APP_EXECUTABLE}"
[[ -x "${RUNTIME_EXECUTABLE}" ]] || die "missing exact runtime executable: ${RUNTIME_EXECUTABLE}"

VERIFY_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-bundle-verify.XXXXXXXX")"
trap cleanup_temp EXIT INT TERM

codesign --verify --deep --strict --verbose=2 "${APP_PATH}"
spctl --assess --type execute --verbose=4 "${APP_PATH}"
xcrun stapler validate "${APP_PATH}"

is_macho() {
  local description
  if ! description="$(file -b -- "$1")"; then
    die "file inspection failed for $1"
  fi
  case "${description}" in
    *Mach-O*) return 0 ;;
    *) return 1 ;;
  esac
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

otool_dependencies() {
  local owner="$1"
  local output
  if ! output="$(otool -L "${owner}")"; then
    die "otool -L failed for ${owner}"
  fi
  if ! printf '%s\n' "${output}" | awk 'NR > 1 { print $1 }'; then
    die "could not parse otool -L output for ${owner}"
  fi
}

otool_rpaths() {
  local owner="$1"
  local output
  if ! output="$(otool -l "${owner}")"; then
    die "otool -l failed for ${owner}"
  fi
  if ! printf '%s\n' "${output}" |
    awk '$1 == "cmd" && $2 == "LC_RPATH" { getline; getline; if ($1 == "path") print $2 }'; then
    die "could not parse LC_RPATH entries for ${owner}"
  fi
}

if ! find "${APP_PATH}/Contents" -type f -print0 >"${VERIFY_TEMP}/bundle-files"; then
  die "find failed while auditing nested bundle files"
fi
while IFS= read -r -d '' candidate; do
  is_macho "${candidate}" || continue
  codesign --verify --strict --verbose=2 "${candidate}"
  if ! otool_dependencies "${candidate}" >"${VERIFY_TEMP}/dependencies"; then
    die "dependency inspection failed for ${candidate}"
  fi
  while IFS= read -r dependency; do
    [[ -n "${dependency}" ]] || continue
    check_dependency "${candidate}" "${dependency}"
  done <"${VERIFY_TEMP}/dependencies"
  if ! otool_rpaths "${candidate}" >"${VERIFY_TEMP}/rpaths"; then
    die "rpath inspection failed for ${candidate}"
  fi
  while IFS= read -r rpath; do
    [[ -n "${rpath}" ]] || continue
    check_dependency "${candidate} LC_RPATH" "${rpath}"
  done <"${VERIFY_TEMP}/rpaths"
done <"${VERIFY_TEMP}/bundle-files"

COLLECTED_PIDS=()
collect_pids() {
  local selector="$1"
  local path="$2"
  local pid
  COLLECTED_PIDS=()
  if ! "${selector}" "${path}" >"${VERIFY_TEMP}/selected-pids"; then
    die "process enumeration failed for ${path}"
  fi
  while IFS= read -r pid; do
    [[ -n "${pid}" ]] && COLLECTED_PIDS[${#COLLECTED_PIDS[@]}]="${pid}"
  done <"${VERIFY_TEMP}/selected-pids"
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
  cleanup_temp
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
      OWNED_PIDS[${#OWNED_PIDS[@]}]="${pid}"
    fi
  done
  for pid in "${runtime_pids[@]}"; do
    : >"${VERIFY_TEMP}/listener-error"
    if "${LSOF_BIN}" -nP -a -p "${pid}" -iTCP -sTCP:LISTEN \
      >"${VERIFY_TEMP}/listeners" 2>"${VERIFY_TEMP}/listener-error"; then
      if grep -q 'TCP 127.0.0.1:' "${VERIFY_TEMP}/listeners"; then
        ready=1
        break 2
      fi
    elif [[ -s "${VERIFY_TEMP}/listener-error" ]]; then
      die "listener enumeration failed for owned PID ${pid}: $(<"${VERIFY_TEMP}/listener-error")"
    elif ! process_executable "${pid}"; then
      continue
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
  if process_executable "${pid}"; then
    case "${PROCESS_EXECUTABLE}" in
      "${APP_EXECUTABLE}"|"${RUNTIME_ROOT}/"*) ;;
      *) continue ;;
    esac
    : >"${VERIFY_TEMP}/listener-error"
    if "${LSOF_BIN}" -nP -a -p "${pid}" -iTCP -sTCP:LISTEN \
      >"${VERIFY_TEMP}/listeners" 2>"${VERIFY_TEMP}/listener-error"; then
      grep -q LISTEN "${VERIFY_TEMP}/listeners" &&
        die "listener remains attributable to captured owned PID ${pid}"
    elif [[ -s "${VERIFY_TEMP}/listener-error" ]]; then
      die "listener enumeration failed for captured PID ${pid}: $(<"${VERIFY_TEMP}/listener-error")"
    fi
  fi
done

printf 'Signed, notarized bundle passed path-scoped launch and shutdown verification: %s\n' "${APP_PATH}"
