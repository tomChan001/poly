#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'verify-bundle: %s\n' "$*" >&2
  exit 1
}

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PS_BIN="ps"
LSOF_BIN="lsof"
VERIFY_TEMP=""
PROCESS_EXECUTABLE=""
MACHO_AUDIT_FIND_BIN="find"
MACHO_AUDIT_FILE_BIN="file"
MACHO_AUDIT_OTOOL_BIN="otool"
MACHO_AUDIT_CODESIGN=0
# shellcheck source=packaging/macos/macho-audit.sh
source "${SCRIPT_DIR}/macho-audit.sh"

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

PID_LOOPBACK_READY=0
audit_runtime_listeners() {
  local pid="$1"
  local lsof_output record listener is_postgres
  PID_LOOPBACK_READY=0
  if ! process_executable "${pid}"; then
    return 1
  fi
  case "${PROCESS_EXECUTABLE}" in
    "${RUNTIME_ROOT}/"*) ;;
    *) die "PID ${pid} executable is outside the exact runtime root: ${PROCESS_EXECUTABLE}" ;;
  esac
  is_postgres=0
  case "${PROCESS_EXECUTABLE}" in
    "${RUNTIME_ROOT}/postgres/bin/postgres"|"${RUNTIME_ROOT}/_internal/postgres/bin/postgres")
      is_postgres=1
      ;;
  esac
  : >"${VERIFY_TEMP}/listener-error"
  if ! lsof_output="$("${LSOF_BIN}" -nP -a -p "${pid}" -iTCP -sTCP:LISTEN -Fn 2>"${VERIFY_TEMP}/listener-error")"; then
    if [[ -n "${lsof_output}" || -s "${VERIFY_TEMP}/listener-error" ]]; then
      die "listener enumeration failed for owned PID ${pid}: $(<"${VERIFY_TEMP}/listener-error")"
    fi
    process_executable "${pid}" || return 1
    return 0
  fi
  [[ ! -s "${VERIFY_TEMP}/listener-error" ]] ||
    die "listener enumeration was incomplete for owned PID ${pid}: $(<"${VERIFY_TEMP}/listener-error")"
  while IFS= read -r record; do
    case "${record}" in
      n*)
        listener="${record#n}"
        listener="${listener#TCP }"
        if [[ "${is_postgres}" -eq 1 ]]; then
          die "PostgreSQL process exposed a TCP listener: PID ${pid} ${listener}"
        fi
        case "${listener}" in
          127.0.0.1:*|\[::1\]:*) PID_LOOPBACK_READY=1 ;;
          *) die "non-loopback TCP listener for owned runtime PID ${pid}: ${listener}" ;;
        esac
        ;;
    esac
  done <<<"${lsof_output}"
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

if [[ "${1:-}" == "--audit-tree" ]]; then
  [[ "$#" -eq 2 && -d "$2" ]] || die "usage: $0 --audit-tree /absolute/tree"
  # Fault-injection overrides are accepted only by this non-signing test mode.
  MACHO_AUDIT_FIND_BIN="${POLY_TEST_FIND:-find}"
  MACHO_AUDIT_FILE_BIN="${POLY_TEST_FILE:-file}"
  MACHO_AUDIT_OTOOL_BIN="${POLY_TEST_OTOOL:-otool}"
  for command_name in awk realpath "${MACHO_AUDIT_FILE_BIN}" "${MACHO_AUDIT_FIND_BIN}" "${MACHO_AUDIT_OTOOL_BIN}"; do
    command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}"
  done
  VERIFY_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-bundle-audit.XXXXXXXX")"
  trap cleanup_temp EXIT INT TERM
  macho_audit_tree "$2" "${VERIFY_TEMP}"
  printf 'Mach-O dependency audit passed: %s\n' "$2"
  exit 0
fi

if [[ "${1:-}" == "--audit-listeners" ]]; then
  [[ "$#" -eq 3 && "$2" == /*/ && "$3" =~ ^[0-9]+$ ]] ||
    die "usage: $0 --audit-listeners /absolute/runtime/ PID"
  # Fault-injection is accepted only by this non-launching diagnostic mode.
  LSOF_BIN="${POLY_TEST_LSOF:-lsof}"
  VERIFY_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/poly-bundle-listeners.XXXXXXXX")"
  trap cleanup_temp EXIT INT TERM
  if ! RUNTIME_ROOT="$(realpath "${2%/}")"; then
    die "could not canonicalize runtime root: $2"
  fi
  audit_runtime_listeners "$3" || die "owned runtime PID vanished during listener audit: $3"
  printf 'Owned runtime listeners are loopback-only and PostgreSQL uses no TCP: %s\n' "$3"
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

MACHO_AUDIT_CODESIGN=1
macho_audit_tree "${APP_PATH}/Contents" "${VERIFY_TEMP}"

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
  attempt_ready=0
  for pid in "${runtime_pids[@]}"; do
    if audit_runtime_listeners "${pid}"; then
      if [[ "${PID_LOOPBACK_READY}" -eq 1 ]]; then
        attempt_ready=1
      fi
    fi
  done
  if [[ "${attempt_ready}" -eq 1 ]]; then
    ready=1
    break
  fi
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
