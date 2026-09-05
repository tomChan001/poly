#!/usr/bin/env bash

# The caller supplies die(), an allowed tree, a work directory, and optional
# command overrides used only by the scripts' explicit diagnostic modes.

macho_is_macho() {
  local description
  if ! description="$("${MACHO_AUDIT_FILE_BIN}" -b -- "$1")"; then
    die "file inspection failed for $1"
  fi
  case "${description}" in
    *Mach-O*) return 0 ;;
    *) return 1 ;;
  esac
}

macho_validate_architecture() {
  local owner="$1"
  local output
  local -a architectures
  if ! output="$("${MACHO_AUDIT_LIPO_BIN}" -archs "${owner}")"; then
    die "lipo architecture inspection failed for ${owner}"
  fi
  read -r -a architectures <<<"${output}"
  [[ "${#architectures[@]}" -eq 1 ]] ||
    die "bundled Mach-O is not thin: ${owner} (${output:-no architectures})"
  [[ "${architectures[0]}" == "${MACHO_AUDIT_EXPECTED_ARCH}" ]] ||
    die "architecture mismatch for ${owner}: expected ${MACHO_AUDIT_EXPECTED_ARCH}, got ${architectures[0]}"
}

macho_otool_minimum_versions() {
  local owner="$1"
  local output
  if ! output="$("${MACHO_AUDIT_OTOOL_BIN}" -l "${owner}")"; then
    die "otool -l failed for ${owner}"
  fi
  if ! printf '%s\n' "${output}" | awk '
    $1 == "cmd" {
      command = $2
      next
    }
    command == "LC_BUILD_VERSION" && $1 == "minos" {
      print $2
      command = ""
      next
    }
    command == "LC_VERSION_MIN_MACOSX" && $1 == "version" {
      print $2
      command = ""
    }
  '; then
    die "could not parse deployment target metadata for ${owner}"
  fi
}

macho_version_at_most() {
  local actual="$1"
  local maximum="$2"
  awk -v actual="${actual}" -v maximum="${maximum}" 'BEGIN {
    actual_parts = split(actual, a, ".")
    maximum_parts = split(maximum, m, ".")
    part_count = actual_parts > maximum_parts ? actual_parts : maximum_parts
    for (part_index = 1; part_index <= part_count; part_index++) {
      actual_part = part_index <= actual_parts ? a[part_index] + 0 : 0
      maximum_part = part_index <= maximum_parts ? m[part_index] + 0 : 0
      if (actual_part < maximum_part) exit 0
      if (actual_part > maximum_part) exit 1
    }
    exit 0
  }'
}

macho_validate_minimum_version() {
  local owner="$1"
  local work_dir="$2"
  local version
  if ! macho_otool_minimum_versions "${owner}" >"${work_dir}/minimum-versions"; then
    die "deployment target inspection failed for ${owner}"
  fi
  [[ -s "${work_dir}/minimum-versions" ]] ||
    die "bundled Mach-O lacks macOS deployment target metadata: ${owner}"
  while IFS= read -r version; do
    [[ "${version}" =~ ^[0-9]+([.][0-9]+){0,3}$ ]] ||
      die "invalid macOS deployment target metadata in ${owner}: ${version}"
    macho_version_at_most "${version}" "${MACHO_AUDIT_MAX_MIN_OS}" ||
      die "macOS deployment target exceeds ${MACHO_AUDIT_MAX_MIN_OS} in ${owner}: ${version}"
  done <"${work_dir}/minimum-versions"
}

macho_otool_dependencies() {
  local owner="$1"
  local output
  if ! output="$("${MACHO_AUDIT_OTOOL_BIN}" -L "${owner}")"; then
    die "otool -L failed for ${owner}"
  fi
  if ! printf '%s\n' "${output}" | awk 'NR > 1 { print $1 }'; then
    die "could not parse otool -L output for ${owner}"
  fi
}

macho_otool_rpaths() {
  local owner="$1"
  local output
  if ! output="$("${MACHO_AUDIT_OTOOL_BIN}" -l "${owner}")"; then
    die "otool -l failed for ${owner}"
  fi
  if ! printf '%s\n' "${output}" |
    awk '$1 == "cmd" && $2 == "LC_RPATH" { getline; getline; if ($1 == "path") print $2 }'; then
    die "could not parse LC_RPATH entries for ${owner}"
  fi
}

macho_require_contained() {
  local owner="$1"
  local source="$2"
  local resolved="$3"
  case "${resolved}" in
    "${MACHO_AUDIT_CANONICAL_ROOT}"|"${MACHO_AUDIT_CANONICAL_ROOT}/"*) return 0 ;;
    *) die "dependency or rpath escapes packaged root in ${owner}: ${source} -> ${resolved}" ;;
  esac
}

macho_executable_directory() {
  local owner="$1"
  local owner_canonical context_root executable context_canonical executable_canonical
  local best_root="" best_executable=""
  [[ -n "${MACHO_AUDIT_CONTEXTS_FILE:-}" && -f "${MACHO_AUDIT_CONTEXTS_FILE}" ]] ||
    die "@executable_path has no declared main executable context in ${owner}"
  if ! owner_canonical="$(realpath "${owner}")"; then
    die "could not canonicalize Mach-O owner: ${owner}"
  fi
  while IFS=$'\t' read -r context_root executable; do
    [[ -n "${context_root}" && -n "${executable}" ]] || continue
    if ! context_canonical="$(realpath "${context_root}")"; then
      die "could not canonicalize executable context root: ${context_root}"
    fi
    case "${owner_canonical}" in
      "${context_canonical}"|"${context_canonical}/"*) ;;
      *) continue ;;
    esac
    if [[ "${#context_canonical}" -gt "${#best_root}" ]]; then
      best_root="${context_canonical}"
      best_executable="${executable}"
    fi
  done <"${MACHO_AUDIT_CONTEXTS_FILE}"
  [[ -n "${best_executable}" ]] ||
    die "@executable_path has no matching main executable context in ${owner}"
  if ! executable_canonical="$(realpath "${best_executable}")"; then
    die "could not canonicalize declared main executable: ${best_executable}"
  fi
  macho_require_contained "${owner}" "main executable" "${executable_canonical}"
  [[ -f "${executable_canonical}" ]] ||
    die "declared main executable does not exist: ${best_executable}"
  dirname -- "${executable_canonical}"
}

macho_resolve_token_path() {
  local owner="$1"
  local value="$2"
  local expected_kind="$3"
  local owner_canonical owner_directory executable_directory suffix candidate resolved
  if ! owner_canonical="$(realpath "${owner}")"; then
    die "could not canonicalize Mach-O owner: ${owner}"
  fi
  macho_require_contained "${owner}" "owner" "${owner_canonical}"
  owner_directory="$(dirname -- "${owner_canonical}")"
  case "${value}" in
    @loader_path)
      candidate="${owner_directory}"
      ;;
    @loader_path/*)
      suffix="${value#@loader_path/}"
      candidate="${owner_directory}/${suffix}"
      ;;
    @executable_path|@executable_path/*)
      if ! executable_directory="$(macho_executable_directory "${owner}")"; then
        die "failed to select main executable context in ${owner}"
      fi
      suffix="${value#@executable_path/}"
      if [[ "${value}" == "@executable_path" ]]; then
        candidate="${executable_directory}"
      else
        candidate="${executable_directory}/${suffix}"
      fi
      ;;
    *)
      die "unexpected dependency or rpath in ${owner}: ${value}"
      ;;
  esac
  if ! resolved="$(realpath "${candidate}" 2>/dev/null)"; then
    die "unresolved ${expected_kind} in ${owner}: ${value}"
  fi
  macho_require_contained "${owner}" "${value}" "${resolved}"
  case "${expected_kind}" in
    LC_RPATH)
      [[ -d "${resolved}" ]] || die "resolved LC_RPATH is not a directory in ${owner}: ${value}"
      ;;
    dependency)
      [[ -f "${resolved}" ]] || die "resolved dependency is not a file in ${owner}: ${value}"
      ;;
  esac
  printf '%s\n' "${resolved}"
}

macho_resolve_rpaths() {
  local owner="$1"
  local input_file="$2"
  local output_file="$3"
  local rpath resolved
  : >"${output_file}"
  while IFS= read -r rpath; do
    [[ -n "${rpath}" ]] || continue
    case "${rpath}" in
      @loader_path|@loader_path/*|@executable_path|@executable_path/*)
        if ! resolved="$(macho_resolve_token_path "${owner}" "${rpath}" LC_RPATH)"; then
          die "failed to resolve LC_RPATH in ${owner}: ${rpath}"
        fi
        printf '%s\n' "${resolved}" >>"${output_file}"
        ;;
      /*)
        die "nonrelocatable absolute LC_RPATH in ${owner}: ${rpath}"
        ;;
      *)
        die "unexpected dependency or rpath in ${owner}: ${rpath}"
        ;;
    esac
  done <"${input_file}"
}

macho_check_dependency() {
  local owner="$1"
  local dependency="$2"
  local resolved_rpaths="$3"
  local rpath candidate resolved found
  case "${dependency}" in
    /usr/lib/*|/System/Library/*|/Library/Apple/System/Library/*)
      case "${dependency}" in
        *"/../"*|*"/./"*) die "unsafe system dependency in ${owner}: ${dependency}" ;;
      esac
      return 0
      ;;
    /opt/homebrew/*|/opt/local/*|/usr/local/*|/Users/*|/runner/*|/home/*|/private/var/folders/*|/Volumes/*)
      die "unsafe dependency in ${owner}: ${dependency}"
      ;;
    /*)
      die "nonrelocatable absolute dependency in ${owner}: ${dependency}"
      ;;
    @loader_path|@loader_path/*|@executable_path|@executable_path/*)
      macho_resolve_token_path "${owner}" "${dependency}" dependency >/dev/null
      ;;
    @rpath/*)
      found=0
      while IFS= read -r rpath; do
        [[ -n "${rpath}" ]] || continue
        candidate="${rpath}/${dependency#@rpath/}"
        [[ -e "${candidate}" ]] || continue
        if ! resolved="$(realpath "${candidate}")"; then
          die "could not canonicalize @rpath dependency in ${owner}: ${dependency}"
        fi
        macho_require_contained "${owner}" "${dependency}" "${resolved}"
        [[ -f "${resolved}" ]] || die "resolved @rpath dependency is not a file in ${owner}: ${dependency}"
        found=1
        break
      done <"${resolved_rpaths}"
      [[ "${found}" -eq 1 ]] || die "unresolved @rpath dependency in ${owner}: ${dependency}"
      ;;
    *)
      die "unexpected dependency or rpath in ${owner}: ${dependency}"
      ;;
  esac
}

macho_audit_tree() {
  local tree="$1"
  local work_dir="$2"
  local candidate canonical_candidate canonical_boundary dependency
  if ! canonical_boundary="$(realpath "${MACHO_AUDIT_BOUNDARY:-${tree}}")"; then
    die "could not canonicalize audit boundary: ${MACHO_AUDIT_BOUNDARY:-${tree}}"
  fi
  [[ -d "${canonical_boundary}" ]] ||
    die "audit boundary is not a directory: ${MACHO_AUDIT_BOUNDARY:-${tree}}"
  if ! MACHO_AUDIT_CANONICAL_ROOT="$(realpath "${tree}")"; then
    die "could not canonicalize packaged root: ${tree}"
  fi
  [[ -d "${MACHO_AUDIT_CANONICAL_ROOT}" ]] || die "packaged root is not a directory: ${tree}"
  if [[ -n "${MACHO_AUDIT_EXPECTED_ARCH:-}" || -n "${MACHO_AUDIT_MAX_MIN_OS:-}" ]]; then
    [[ -n "${MACHO_AUDIT_EXPECTED_ARCH:-}" && -n "${MACHO_AUDIT_MAX_MIN_OS:-}" ]] ||
      die "architecture and deployment-target audit settings must be supplied together"
    [[ "${MACHO_AUDIT_EXPECTED_ARCH}" == "arm64" || "${MACHO_AUDIT_EXPECTED_ARCH}" == "x86_64" ]] ||
      die "unsupported expected Mach-O architecture: ${MACHO_AUDIT_EXPECTED_ARCH}"
  fi
  case "${MACHO_AUDIT_CANONICAL_ROOT}" in
    "${canonical_boundary}"|"${canonical_boundary}/"*) ;;
    *)
      die "packaged root escapes audit boundary: ${tree} -> ${MACHO_AUDIT_CANONICAL_ROOT}"
      ;;
  esac
  if ! "${MACHO_AUDIT_FIND_BIN}" "${tree}" \( -type f -o -type l \) -print0 >"${work_dir}/audit-files"; then
    die "find failed while auditing ${tree}"
  fi
  while IFS= read -r -d '' candidate; do
    if ! canonical_candidate="$(realpath "${candidate}")"; then
      die "could not canonicalize packaged file: ${candidate}"
    fi
    macho_require_contained "${candidate}" "owner" "${canonical_candidate}"
    if [[ -d "${canonical_candidate}" ]]; then
      # Directory symlinks are checked for containment above. Their actual
      # contents are inventoried at the in-tree target rather than traversed
      # through the alias.
      continue
    fi
    [[ -f "${canonical_candidate}" ]] || die "packaged file does not exist: ${candidate}"
    # Static archives and ordinary resource files are explicit exceptions: file(1)
    # does not identify them as Mach-O. System dylibs are dependencies, never
    # bundled candidates, and are handled by macho_check_dependency().
    macho_is_macho "${candidate}" || continue
    if [[ -n "${MACHO_AUDIT_EXPECTED_ARCH:-}" ]]; then
      macho_validate_architecture "${candidate}"
      macho_validate_minimum_version "${candidate}" "${work_dir}"
    fi
    if [[ "${MACHO_AUDIT_CODESIGN:-0}" -eq 1 ]]; then
      "${MACHO_AUDIT_CODESIGN_BIN}" --verify --strict --verbose=2 "${candidate}"
    fi
    macho_otool_dependencies "${candidate}" >"${work_dir}/dependencies"
    macho_otool_rpaths "${candidate}" >"${work_dir}/rpaths"
    macho_resolve_rpaths "${candidate}" "${work_dir}/rpaths" "${work_dir}/resolved-rpaths"
    while IFS= read -r dependency; do
      [[ -n "${dependency}" ]] || continue
      macho_check_dependency "${candidate}" "${dependency}" "${work_dir}/resolved-rpaths"
    done <"${work_dir}/dependencies"
  done <"${work_dir}/audit-files"
}
