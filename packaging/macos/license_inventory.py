"""Fail-closed license attribution for native files in the macOS runtime."""

from __future__ import annotations

import re
from pathlib import Path
from typing import AbstractSet, Mapping, Sequence


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
) -> str | None:
    normalized_production = {
        normalize_name(name): details
        for name, details in production_distributions.items()
    }
    first_component = bundled_path.split("/", 1)[0]
    module_name = first_component.split(".", 1)[0]
    owners = {
        name
        for name in package_distributions.get(module_name, ())
        if normalize_name(name) in normalized_production
    }
    if first_component.endswith((".dist-info", ".egg-info")):
        normalized_component = normalize_name(first_component)
        owners.update(
            name
            for name in production_distributions
            if normalized_component.startswith(f"{normalize_name(name)}-")
        )
    if not owners:
        return None
    evidence: list[str] = []
    for name in sorted(owners, key=str.casefold):
        details = normalized_production[normalize_name(name)]
        if not isinstance(details, Mapping):
            raise ValueError(f"Python package inventory lacks details: {name}")
        license_id = details.get("license")
        if not license_id:
            raise ValueError(f"Python package inventory lacks license evidence: {name}")
        evidence.append(f"{name} ({license_id})")
    return "Python production dependency: " + ", ".join(evidence)


def classify_macho_paths(
    relative_paths: Sequence[str],
    *,
    production_distributions: Mapping[str, object],
    package_distributions: Mapping[str, Sequence[str]],
    stdlib_modules: AbstractSet[str],
    native_license_root: Path,
) -> dict[str, str]:
    """Map every Mach-O path to license evidence or fail on an unknown file."""

    classifications: dict[str, str] = {}
    for relative in relative_paths:
        _validate_relative(relative)
        bundled_path = _without_contents_directory(relative)
        if bundled_path == "poly-runtime":
            classifications[relative] = "Poly launcher / PyInstaller bootloader"
            continue
        if bundled_path.startswith("postgres/"):
            classifications[relative] = "PostgreSQL"
            continue
        filename = Path(bundled_path).name
        module_name = bundled_path.split("/", 1)[0].split(".", 1)[0]
        if (
            filename.startswith(("libpython", "python"))
            and filename.endswith(".dylib")
        ) or bundled_path.startswith("lib-dynload/") or (
            relative.endswith(".so") and module_name in stdlib_modules
        ):
            classifications[relative] = "CPython"
            continue

        package_attribution = _package_attribution(
            bundled_path,
            production_distributions=production_distributions,
            package_distributions=package_distributions,
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
    stdlib_modules: AbstractSet[str],
) -> dict[str, str]:
    """Map every packaged runtime file to explicit ownership evidence."""

    classifications: dict[str, str] = {}
    for relative in relative_paths:
        _validate_relative(relative)
        if relative in macho_classifications:
            classifications[relative] = macho_classifications[relative]
            continue
        bundled_path = _without_contents_directory(relative)
        first_component = bundled_path.split("/", 1)[0]
        module_name = first_component.split(".", 1)[0]
        if bundled_path.startswith("postgres/"):
            classifications[relative] = "PostgreSQL"
        elif bundled_path == "alembic.ini" or bundled_path.startswith(
            ("migrations/", "frontend/dist/")
        ):
            classifications[relative] = "Poly application asset"
        elif bundled_path == "base_library.zip" or bundled_path.startswith(
            ("lib-dynload/", "python3.")
        ) or module_name in stdlib_modules:
            classifications[relative] = "CPython"
        elif bundled_path.startswith(("pyimod", "pyi_rth_", "_pyi_rth_utils/")):
            classifications[relative] = "PyInstaller runtime support"
        else:
            package_attribution = _package_attribution(
                bundled_path,
                production_distributions=production_distributions,
                package_distributions=package_distributions,
            )
            if package_attribution is None:
                raise ValueError(
                    f"packaged file lacks license inventory: {relative}"
                )
            classifications[relative] = package_attribution
    return classifications
