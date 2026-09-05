"""Fail-closed license attribution for native files in the macOS runtime."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _without_contents_directory(relative: str) -> str:
    return relative.removeprefix("_internal/")


def _validate_relative(relative: str) -> None:
    path = Path(relative)
    if path.is_absolute() or not relative or ".." in path.parts:
        raise ValueError(f"invalid packaged relative path: {relative}")


def _package_attribution(
    bundled_path: str,
    *,
    production_distributions: Mapping[str, object],
    package_distributions: Mapping[str, Sequence[str]],
    distribution_files: Mapping[str, AbstractSet[str]],
) -> str | None:
    normalized_production = {
        normalize_name(name): details
        for name, details in production_distributions.items()
    }
    first_component = bundled_path.split("/", 1)[0]
    module_name = first_component.split(".", 1)[0]
    possible_owners = {
        name
        for name in package_distributions.get(module_name, ())
        if normalize_name(name) in normalized_production
    }
    if first_component.endswith((".dist-info", ".egg-info")):
        normalized_component = normalize_name(first_component)
        possible_owners.update(
            name
            for name in production_distributions
            if normalized_component.startswith(f"{normalize_name(name)}-")
        )
    owners = {
        name
        for name in possible_owners
        if bundled_path
        in {
            file_path.replace("\\", "/")
            for file_path in distribution_files.get(
                normalize_name(name), distribution_files.get(name, set())
            )
        }
    }
    if not owners:
        return None
    evidence: list[str] = []
    for name in sorted(owners, key=str.casefold):
        details = normalized_production[normalize_name(name)]
        if not isinstance(details, Mapping):
            raise TypeError(f"Python package inventory lacks details: {name}")
        license_id = details.get("license")
        if not license_id:
            raise ValueError(f"Python package inventory lacks license evidence: {name}")
        evidence.append(f"{name} ({license_id})")
    return "Python production dependency: " + ", ".join(evidence)


def _cpython_manifest_path(bundled_path: str) -> str:
    without_prefix = re.sub(r"^python\d+\.\d+/", "", bundled_path)
    if "/__pycache__/" in without_prefix and without_prefix.endswith(".pyc"):
        directory, filename = without_prefix.rsplit("/__pycache__/", 1)
        source_name = filename.split(".", 1)[0] + ".py"
        return f"{directory}/{source_name}"
    if without_prefix.endswith(".pyc"):
        return without_prefix[:-1]
    return without_prefix


def _is_cpython_file(
    bundled_path: str, *, stdlib_files: AbstractSet[str]
) -> bool:
    return _cpython_manifest_path(bundled_path) in stdlib_files


def classify_macho_paths(
    relative_paths: Sequence[str],
    *,
    production_distributions: Mapping[str, object],
    package_distributions: Mapping[str, Sequence[str]],
    distribution_files: Mapping[str, AbstractSet[str]],
    stdlib_files: AbstractSet[str],
    cpython_library_files: AbstractSet[str],
    native_license_root: Path,
) -> dict[str, str]:
    """Map every Mach-O path to license evidence or fail on an unknown file."""

    classifications: dict[str, str] = {}
    normalized_stdlib_files = frozenset(
        path.replace("\\", "/") for path in stdlib_files
    )
    normalized_cpython_libraries = frozenset(
        path.replace("\\", "/") for path in cpython_library_files
    )
    for relative in relative_paths:
        _validate_relative(relative)
        bundled_path = _without_contents_directory(relative)
        if bundled_path == "poly-runtime":
            classifications[relative] = "Poly launcher / PyInstaller bootloader"
            continue
        if bundled_path.startswith("postgres/"):
            classifications[relative] = "PostgreSQL"
            continue
        if relative in normalized_cpython_libraries or (
            relative.endswith(".so")
            and _is_cpython_file(
                bundled_path, stdlib_files=normalized_stdlib_files
            )
        ):
            classifications[relative] = "CPython"
            continue

        package_attribution = _package_attribution(
            bundled_path,
            production_distributions=production_distributions,
            package_distributions=package_distributions,
            distribution_files=distribution_files,
        )
        if relative.endswith(".so") and package_attribution:
            classifications[relative] = package_attribution
            continue

        explicit_license = native_license_root / f"{relative}.LICENSE"
        if not explicit_license.is_file():
            raise ValueError(
                f"packaged native file lacks inventory: {relative}; "
                f"add {explicit_license}"
            )
        classifications[relative] = (
            f"Native library with explicit notice: {relative}.LICENSE"
        )
    return classifications


def classify_runtime_paths(
    relative_paths: Sequence[str],
    *,
    macho_classifications: Mapping[str, str],
    production_distributions: Mapping[str, object],
    package_distributions: Mapping[str, Sequence[str]],
    distribution_files: Mapping[str, AbstractSet[str]],
    stdlib_files: AbstractSet[str],
    pyinstaller_runtime_files: AbstractSet[str],
) -> dict[str, str]:
    """Map every packaged runtime file to explicit ownership evidence."""

    classifications: dict[str, str] = {}
    normalized_stdlib_files = frozenset(
        path.replace("\\", "/") for path in stdlib_files
    )
    normalized_pyinstaller_files = frozenset(
        path.replace("\\", "/") for path in pyinstaller_runtime_files
    )
    for relative in relative_paths:
        _validate_relative(relative)
        if relative in macho_classifications:
            classifications[relative] = macho_classifications[relative]
            continue
        bundled_path = _without_contents_directory(relative)
        if bundled_path.startswith("postgres/"):
            classifications[relative] = "PostgreSQL"
        elif bundled_path == "alembic.ini" or bundled_path.startswith(
            ("migrations/", "frontend/dist/")
        ):
            classifications[relative] = "Poly application asset"
        elif bundled_path == "base_library.zip" or _is_cpython_file(
            bundled_path, stdlib_files=normalized_stdlib_files
        ):
            classifications[relative] = "CPython"
        elif bundled_path in normalized_pyinstaller_files:
            classifications[relative] = "PyInstaller runtime support"
        else:
            package_attribution = _package_attribution(
                bundled_path,
                production_distributions=production_distributions,
                package_distributions=package_distributions,
                distribution_files=distribution_files,
            )
            if package_attribution is None:
                raise ValueError(
                    f"packaged file lacks license inventory: {relative}"
                )
            classifications[relative] = package_attribution
    return classifications
