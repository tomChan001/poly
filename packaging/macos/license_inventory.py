"""Fail-closed license attribution for native files in the macOS runtime."""

from __future__ import annotations

import re
from pathlib import Path
from typing import AbstractSet, Mapping, Sequence


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _without_contents_directory(relative: str) -> str:
    return relative.removeprefix("_internal/")


def classify_macho_paths(
    relative_paths: Sequence[str],
    *,
    production_distributions: Mapping[str, object],
    package_distributions: Mapping[str, Sequence[str]],
    stdlib_modules: AbstractSet[str],
    native_license_root: Path,
) -> dict[str, str]:
    """Map every Mach-O path to license evidence or fail on an unknown file."""

    normalized_production = {
        normalize_name(name): details
        for name, details in production_distributions.items()
    }
    classifications: dict[str, str] = {}
    for relative in relative_paths:
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

        matching_distributions = sorted(
            {
                name
                for name in package_distributions.get(module_name, ())
                if normalize_name(name) in normalized_production
            },
            key=str.casefold,
        )
        if relative.endswith(".so") and matching_distributions:
            classifications[relative] = (
                "Python production dependency: "
                + ", ".join(matching_distributions)
            )
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
