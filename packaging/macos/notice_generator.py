"""Deterministically generate macOS third-party notices from locked inputs."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import re
import sys
import sysconfig
import tomllib
from collections.abc import Set as AbstractSet
from pathlib import Path

from license_inventory import (
    classify_macho_paths,
    classify_runtime_paths,
    normalize_name,
)


def load_requirement_api():
    """Load the third-party packaging distribution without name shadowing."""

    alias = "_poly_notice_packaging"
    package = sys.modules.get(alias)
    if package is None:
        distribution = importlib.metadata.distribution("packaging")
        package_directory = Path(distribution.locate_file("packaging")).resolve()
        init_path = package_directory / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            alias,
            init_path,
            submodule_search_locations=[str(package_directory)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"could not load the third-party packaging distribution from {init_path}"
            )
        package = importlib.util.module_from_spec(spec)
        sys.modules[alias] = package
        try:
            spec.loader.exec_module(package)
        except BaseException:
            sys.modules.pop(alias, None)
            raise
    markers = importlib.import_module(f"{alias}.markers")
    requirements = importlib.import_module(f"{alias}.requirements")
    licenses = importlib.import_module(f"{alias}.licenses")
    return (
        markers.default_environment,
        requirements.Requirement,
        licenses.canonicalize_license_expression,
    )


default_environment, Requirement, canonicalize_license_expression = load_requirement_api()


POSTGRESQL_LICENSE = """PostgreSQL Database Management System
(also known as Postgres, formerly known as Postgres95)

Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group

Portions Copyright (c) 1994, The Regents of the University of California

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement
is hereby granted, provided that the above copyright notice and this
paragraph and the following two paragraphs appear in all copies.

IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA BE LIABLE TO ANY PARTY FOR DIRECT,
INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST PROFITS,
ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF THE
UNIVERSITY OF CALIFORNIA HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

THE UNIVERSITY OF CALIFORNIA SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING,
BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE.  THE SOFTWARE PROVIDED HEREUNDER IS ON AN "AS IS" BASIS, AND
THE UNIVERSITY OF CALIFORNIA HAS NO OBLIGATIONS TO PROVIDE MAINTENANCE, SUPPORT,
UPDATES, ENHANCEMENTS, OR MODIFICATIONS."""

# Portable fallback for a macOS-only locked wheel that cannot be installed in a
# Windows check environment.  This value is from the versioned PyPI metadata:
# https://pypi.org/project/uvloop/0.22.1/
PORTABLE_PYTHON_LICENSES = {
    ("uvloop", "0.22.1"): "MIT OR Apache-2.0",
}


def clean(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).rstrip()


def fenced(text: str) -> str:
    return f"```text\n{clean(text)}\n```"


def classifier_license(classifiers: list[str]) -> str:
    labels = [value.removeprefix("License :: ") for value in classifiers]
    mapping = {
        "OSI Approved :: MIT License": "MIT",
        "OSI Approved :: Apache Software License": "Apache-2.0",
        "OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
        "OSI Approved :: BSD License": "BSD",
        "Public Domain": "Public-Domain",
    }
    return " OR ".join(mapping.get(label, label) for label in labels)


def locked_python_source(lock_entry: dict[str, object]) -> str:
    sdist = lock_entry.get("sdist")
    if isinstance(sdist, dict) and sdist.get("url") and sdist.get("hash"):
        return f"{sdist['url']}#{sdist['hash']}"
    source = json.dumps(
        lock_entry.get("source", {}), sort_keys=True, separators=(",", ":")
    )
    if source == "{}":
        raise ValueError("Python package lacks locked source attribution")
    return source


def production_requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        requirement = Requirement(line.rstrip(" \\"))
        environments = []
        for machine in ("arm64", "x86_64"):
            environment = default_environment()
            environment.update(
                sys_platform="darwin",
                platform_system="Darwin",
                platform_machine=machine,
            )
            environments.append(environment)
        if requirement.marker is None or any(
            requirement.marker.evaluate(environment) for environment in environments
        ):
            names.add(normalize_name(requirement.name))
    if not names:
        raise ValueError("frozen Python production export contained no packages")
    return names


def python_inventory(requirements: Path, uv_lock: Path) -> list[dict[str, str]]:
    production_names = production_requirement_names(requirements)
    locked = {
        (normalize_name(package["name"]), package["version"]): package
        for package in tomllib.loads(uv_lock.read_text(encoding="utf-8"))["package"]
        if "version" in package
    }
    packages: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "")
        normalized = normalize_name(name)
        if normalized not in production_names:
            continue
        version = distribution.version
        lock_entry = locked.get((normalized, version))
        if lock_entry is None:
            raise ValueError(f"installed Python package is not locked: {name} {version}")
        license_id = distribution.metadata.get("License-Expression")
        classifiers = [
            value
            for value in distribution.metadata.get_all("Classifier", [])
            if value.startswith("License :: ")
        ]
        if not license_id and classifiers:
            license_id = classifier_license(classifiers)
        if not license_id:
            license_id = distribution.metadata.get("License")
        if not license_id:
            raise ValueError(f"Python package lacks license metadata: {name} {version}")
        source = locked_python_source(lock_entry)
        packages.append(
            {"name": name, "version": version, "license": license_id.strip(), "source": source}
        )
    found = {normalize_name(package["name"]) for package in packages}
    missing = sorted(production_names - found)
    for normalized in missing:
        matching = [key for key in locked if key[0] == normalized]
        if len(matching) != 1:
            raise ValueError(f"locked Python package version is ambiguous: {normalized}")
        name, version = matching[0]
        license_id = PORTABLE_PYTHON_LICENSES.get((name, version))
        if license_id is None:
            continue
        lock_entry = locked[(name, version)]
        source = locked_python_source(lock_entry)
        packages.append(
            {"name": name, "version": version, "license": license_id, "source": source}
        )
    found = {normalize_name(package["name"]) for package in packages}
    missing = sorted(production_names - found)
    if missing:
        raise ValueError(
            "frozen Python packages lack installed or portable license metadata: "
            + ", ".join(missing)
        )
    return sorted(packages, key=lambda package: (package["name"].casefold(), package["version"]))


def installed_distribution_files(names: set[str]) -> dict[str, frozenset[str]]:
    wanted = {normalize_name(name) for name in names}
    inventory: dict[str, frozenset[str]] = {}
    for distribution in importlib.metadata.distributions():
        name = normalize_name(distribution.metadata.get("Name", ""))
        if name not in wanted:
            continue
        files = distribution.files
        if not files:
            raise ValueError(f"installed distribution has no RECORD/file inventory: {name}")
        inventory[name] = frozenset(
            str(path).replace("\\", "/") for path in files
        )
    missing = sorted(wanted - inventory.keys())
    if missing:
        raise ValueError(
            "installed distributions lack RECORD/file inventory: "
            + ", ".join(missing)
        )
    return inventory


def cpython_stdlib_files() -> frozenset[str]:
    stdlib_root = Path(sysconfig.get_path("stdlib")).resolve()
    if not stdlib_root.is_dir():
        raise ValueError(f"CPython standard library root is missing: {stdlib_root}")
    return frozenset(
        path.relative_to(stdlib_root).as_posix()
        for path in stdlib_root.rglob("*")
        if path.is_file()
    )


def cpython_library_files() -> frozenset[str]:
    names = {
        Path(value).name
        for key in ("LDLIBRARY", "INSTSONAME")
        if (value := sysconfig.get_config_var(key))
        and str(value).endswith(".dylib")
    }
    if not names:
        raise ValueError("CPython build metadata lacks its macOS dynamic library name")
    return frozenset(names)


def pyinstaller_runtime_files(
    distribution_files: AbstractSet[str],
) -> frozenset[str]:
    runtime_files: set[str] = set()
    for raw_path in distribution_files:
        path = raw_path.replace("\\", "/")
        match = re.fullmatch(r"PyInstaller/loader/(pyimod\d+_[^/]+)\.py", path)
        if match:
            runtime_files.add(f"{match.group(1)}.pyc")
            continue
        match = re.fullmatch(r"PyInstaller/hooks/rthooks/(pyi_rth_[^/]+)\.py", path)
        if match:
            runtime_files.update({f"{match.group(1)}.py", f"{match.group(1)}.pyc"})
            continue
        prefix = "PyInstaller/fake-modules/"
        if path.startswith(f"{prefix}_pyi_rth_utils/") and path.endswith(".py"):
            relative = path.removeprefix(prefix)
            runtime_files.update({relative, f"{relative}c"})
    if not runtime_files:
        raise ValueError("PyInstaller distribution lacks its runtime support manifest")
    return frozenset(runtime_files)


def rust_inventory(metadata_paths: list[Path]) -> tuple[list[dict[str, str]], Path]:
    selected: dict[tuple[str, str], dict[str, str]] = {}
    tauri_root: Path | None = None
    for metadata_path in metadata_paths:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        packages = {package["id"]: package for package in metadata["packages"]}
        nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
        roots = [
            package["id"]
            for package in metadata["packages"]
            if package["name"] == "poly-desktop"
        ]
        pending = list(roots)
        visited: set[str] = set()
        while pending:
            package_id = pending.pop()
            if package_id in visited:
                continue
            visited.add(package_id)
            node = nodes.get(package_id)
            if node is None:
                continue
            for dependency in node.get("deps", []):
                if any(kind.get("kind") != "dev" for kind in dependency.get("dep_kinds", [])):
                    pending.append(dependency["pkg"])
        for package_id in visited:
            package = packages[package_id]
            if package["name"] == "poly-desktop":
                continue
            license_id = package.get("license")
            source = package.get("repository") or package.get("source")
            if not license_id:
                raise ValueError(f"Rust package lacks license metadata: {package['name']} {package['version']}")
            if not source:
                raise ValueError(f"Rust package lacks source attribution: {package['name']} {package['version']}")
            selected[(package["name"], package["version"])] = {
                "name": package["name"],
                "version": package["version"],
                "license": license_id,
                "source": source,
            }
            if package["name"] == "tauri":
                tauri_root = Path(package["manifest_path"]).parent
    if not selected or tauri_root is None:
        raise ValueError("locked macOS Rust inventory or Tauri package was not resolved")
    return [selected[key] for key in sorted(selected, key=lambda item: (item[0].casefold(), item[1]))], tauri_root


def npm_inventory(lock_path: Path) -> list[dict[str, str]]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    packages: list[dict[str, str]] = []
    for package_path, package in lock.get("packages", {}).items():
        if not package_path or package.get("dev") is True:
            continue
        name = package.get("name") or package_path.rsplit("node_modules/", 1)[-1]
        license_id = package.get("license")
        source = package.get("resolved")
        if not license_id or not source:
            raise ValueError(f"npm package lacks license/source metadata: {name} {package.get('version', 'unknown')}")
        packages.append(
            {"name": name, "version": package["version"], "license": license_id, "source": source}
        )
    return sorted(packages, key=lambda package: (package["name"].casefold(), package["version"]))


LICENSE_LABEL_ALIASES = (
    ("Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
    ("GNU General Public License v2 (GPLv2)", "GPL-2.0-only"),
    ("The MIT License (MIT)", "MIT"),
    ("Apache Software License", "Apache-2.0"),
    ("Apache 2.0", "Apache-2.0"),
    ("MIT License", "MIT"),
    ("BSD License", "LicenseRef-BSD-Unspecified"),
    ("Public-Domain", "LicenseRef-Public-Domain"),
    ("Public Domain", "LicenseRef-Public-Domain"),
)


def normalized_license_expression(expression: str) -> tuple[object, ...]:
    """Return a commutative syntax tree for a validated SPDX expression."""

    normalized = " ".join(expression.strip().split())
    if not normalized or normalized.casefold() in {"none", "unknown", "n/a"}:
        raise ValueError(f"unknown license expression: {expression!r}")
    normalized = normalized.replace(";", " OR ")
    for label, replacement in LICENSE_LABEL_ALIASES:
        normalized = re.sub(
            re.escape(label), replacement, normalized, flags=re.IGNORECASE
        )
    normalized = re.sub(
        r"(?<![-A-Za-z0-9])BSD(?![-A-Za-z0-9])",
        "LicenseRef-BSD-Unspecified",
        normalized,
        flags=re.IGNORECASE,
    )
    try:
        canonical = str(canonicalize_license_expression(normalized))
    except Exception as error:
        raise ValueError(
            f"invalid or unknown license expression {expression!r}: {error}"
        ) from error

    tokens = re.findall(r"\(|\)|[^\s()]+", canonical)
    position = 0

    def combine(operator: str, left: tuple[object, ...], right: tuple[object, ...]):
        children: list[tuple[object, ...]] = []
        for node in (left, right):
            if node[0] == operator:
                children.extend(node[1:])
            else:
                children.append(node)
        return (operator, *sorted(children, key=repr))

    def parse_primary() -> tuple[object, ...]:
        nonlocal position
        if tokens[position] == "(":
            position += 1
            node = parse_or()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError(f"unbalanced canonical license expression: {canonical}")
            position += 1
            return node
        token = tokens[position]
        position += 1
        return ("LICENSE", token)

    def parse_with() -> tuple[object, ...]:
        nonlocal position
        node = parse_primary()
        if position < len(tokens) and tokens[position] == "WITH":
            position += 1
            exception = tokens[position]
            position += 1
            return ("WITH", node, exception)
        return node

    def parse_and() -> tuple[object, ...]:
        nonlocal position
        node = parse_with()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            node = combine("AND", node, parse_with())
        return node

    def parse_or() -> tuple[object, ...]:
        nonlocal position
        node = parse_and()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            node = combine("OR", node, parse_and())
        return node

    tree = parse_or()
    if position != len(tokens):
        raise ValueError(f"could not parse canonical license expression: {canonical}")
    return tree


def validate_external_inventory(
    path: Path,
    expected: list[dict[str, str]],
    *,
    ecosystem: str,
) -> None:
    """Compare an optional license tool report with the deterministic inventory."""
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, list):
        raise ValueError(f"{ecosystem} license report is not a JSON array")
    actual: dict[tuple[str, str], str] = {}
    for entry in report:
        name = entry.get("name") or entry.get("Name")
        version = entry.get("version") or entry.get("Version")
        license_id = entry.get("license") or entry.get("License")
        if name and version:
            actual[(normalize_name(str(name)), str(version))] = str(license_id or "").strip()
    missing: list[str] = []
    mismatched: list[str] = []
    for package in expected:
        key = (normalize_name(package["name"]), package["version"])
        external_license = actual.get(key)
        label = f"{package['name']} {package['version']}"
        if external_license is None:
            missing.append(label)
        elif not external_license:
            mismatched.append(f"{label} (empty external license)")
        else:
            try:
                expected_terms = normalized_license_expression(package["license"])
                external_terms = normalized_license_expression(external_license)
            except ValueError as error:
                mismatched.append(f"{label} ({error})")
            else:
                if expected_terms != external_terms:
                    mismatched.append(
                        f"{label} ({external_license!r} conflicts with "
                        f"{package['license']!r})"
                    )
    if ecosystem == "Python":
        missing = [
            label
            for label in missing
            if (normalize_name(label.rsplit(" ", 1)[0]), label.rsplit(" ", 1)[1])
            not in PORTABLE_PYTHON_LICENSES
        ]
    if missing or mismatched:
        details = "; ".join(
            part
            for part in (
                f"missing: {', '.join(missing)}" if missing else "",
                f"license mismatch: {', '.join(mismatched)}" if mismatched else "",
            )
            if part
        )
        raise ValueError(f"{ecosystem} license report differs from lock inventory: {details}")


def all_license_files(root: Path) -> list[Path]:
    files = sorted(
        {
            path
            for pattern in ("LICENSE*", "COPYING*", "NOTICE*")
            for path in root.glob(pattern)
            if path.is_file()
        },
        key=lambda path: path.name.casefold(),
    )
    if not files:
        raise ValueError(f"required license text is missing under {root}")
    return files


def append_inventory(parts: list[str], title: str, packages: list[dict[str, str]]) -> None:
    parts.extend(["", f"## {title}", "", "| Package | Version | License | Locked source |", "| --- | --- | --- | --- |"])
    for package in packages:
        parts.append(
            f"| `{package['name']}` | `{package['version']}` | {package['license']} | `{package['source']}` |"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rust-metadata", action="append", required=True, type=Path)
    parser.add_argument("--python-requirements", required=True, type=Path)
    parser.add_argument("--uv-lock", required=True, type=Path)
    parser.add_argument("--npm-lock", required=True, type=Path)
    parser.add_argument("--cpython-license", required=True, type=Path)
    parser.add_argument("--pyinstaller-license", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--macho-inventory", type=Path)
    parser.add_argument("--native-license-root", type=Path)
    parser.add_argument("--cargo-license", type=Path)
    parser.add_argument("--pip-licenses", type=Path)
    args = parser.parse_args()

    rust, tauri_root = rust_inventory(args.rust_metadata)
    python = python_inventory(args.python_requirements, args.uv_lock)
    npm = npm_inventory(args.npm_lock)
    if args.cargo_license is not None:
        validate_external_inventory(args.cargo_license, rust, ecosystem="Rust")
    if args.pip_licenses is not None:
        validate_external_inventory(args.pip_licenses, python, ecosystem="Python")
    parts = [
        "# Third-Party Notices",
        "",
        "Generated deterministically by `packaging/macos/generate-notices.sh` from committed lockfiles and installed artifacts resolved by those locks.",
        "",
        "This is a reproducible attribution and notice artifact, not legal advice. Assembly verification adds an exact native file inventory and fails closed when attribution evidence is missing.",
        "",
        "## Application notice",
        "",
        "Poly is the top-level application. This document records third-party material used by or distributed with it; it does not grant a license to Poly itself.",
    ]
    append_inventory(parts, "Locked Rust production dependencies", rust)
    append_inventory(parts, "Locked Python production dependencies", python)
    append_inventory(parts, "Locked npm production dependencies", npm)

    parts.extend(["", "## CPython license", "", fenced(args.cpython_license.read_text(encoding="utf-8", errors="replace"))])
    parts.extend(["", "## PyInstaller bootloader license", "", fenced(args.pyinstaller_license.read_text(encoding="utf-8", errors="replace"))])
    parts.extend(["", "## PostgreSQL License", "", fenced(POSTGRESQL_LICENSE)])
    parts.extend(["", "## Tauri license texts"])
    for license_path in all_license_files(tauri_root):
        parts.extend(["", f"### {license_path.name}", "", fenced(license_path.read_text(encoding="utf-8", errors="replace"))])
    parts.extend([
        "",
        "## WebKit and macOS native components",
        "",
        "Poly uses Tauri's macOS WebView integration and the WebKit framework supplied by macOS. WebKit and Apple system libraries are dynamically used system components and are not copied into the application bundle. Any non-system Mach-O copied into the runtime must be attributed by the assembly inventory below or generation fails.",
    ])

    if args.runtime is not None:
        if args.macho_inventory is None or args.native_license_root is None:
            raise ValueError("assembled mode requires Mach-O inventory and native license root")
        macho_paths = args.macho_inventory.read_text(encoding="utf-8").splitlines()
        production = {package["name"]: package for package in python}
        distribution_files = installed_distribution_files(set(production))
        stdlib_files = cpython_stdlib_files()
        cpython_libraries = cpython_library_files()
        pyinstaller_files = installed_distribution_files({"PyInstaller"})[
            normalize_name("PyInstaller")
        ]
        pyinstaller_manifest = pyinstaller_runtime_files(pyinstaller_files)
        classifications = classify_macho_paths(
            macho_paths,
            production_distributions=production,
            package_distributions=importlib.metadata.packages_distributions(),
            distribution_files=distribution_files,
            stdlib_files=stdlib_files,
            cpython_library_files=cpython_libraries,
            native_license_root=args.native_license_root,
        )
        runtime_paths = sorted(
            (
                path.relative_to(args.runtime).as_posix()
                for path in args.runtime.rglob("*")
                if path.is_file()
            ),
            key=str.casefold,
        )
        file_classifications = classify_runtime_paths(
            runtime_paths,
            macho_classifications=classifications,
            production_distributions=production,
            package_distributions=importlib.metadata.packages_distributions(),
            distribution_files=distribution_files,
            stdlib_files=stdlib_files,
            pyinstaller_runtime_files=pyinstaller_manifest,
        )
        parts.extend(["", "## Assembled native file inventory", "", "| Packaged file | License attribution |", "| --- | --- |"])
        for relative in runtime_paths:
            parts.append(
                f"| `poly-runtime/{relative}` | {file_classifications[relative]} |"
            )
        if args.native_license_root.is_dir():
            for license_path in sorted(args.native_license_root.rglob("*.LICENSE"), key=lambda path: path.relative_to(args.native_license_root).as_posix().casefold()):
                relative = license_path.relative_to(args.native_license_root).as_posix()
                parts.extend(["", f"### Native license: {relative}", "", fenced(license_path.read_text(encoding="utf-8", errors="replace"))])

    args.output.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"notice-generator: {error}", file=sys.stderr)
        raise SystemExit(1)
