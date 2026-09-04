# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH).resolve().parents[1]
TARGET_ARCH = os.environ.get("POLY_TARGET_ARCH")
TARGET_TRIPLE = os.environ.get("POLY_TARGET_TRIPLE")
SUPPORTED_TARGETS = {
    "arm64": "aarch64-apple-darwin",
    "x86_64": "x86_64-apple-darwin",
}

if TARGET_ARCH not in SUPPORTED_TARGETS:
    raise SystemExit("POLY_TARGET_ARCH must be either 'arm64' or 'x86_64'")
if TARGET_TRIPLE != SUPPORTED_TARGETS[TARGET_ARCH]:
    raise SystemExit(
        "POLY_TARGET_TRIPLE must match POLY_TARGET_ARCH "
        f"({TARGET_ARCH!r} requires {SUPPORTED_TARGETS[TARGET_ARCH]!r})"
    )

ENTRY_POINT = ROOT / "backend" / "app" / "desktop" / "__main__.py"
MIGRATIONS = ROOT / "migrations"
ALEMBIC_CONFIG = ROOT / "alembic.ini"
FRONTEND_DIST = ROOT / "frontend" / "dist"
POSTGRES_DIST = ROOT / "packaging" / "macos" / "postgres" / TARGET_TRIPLE

required_resources = (
    ENTRY_POINT,
    MIGRATIONS / "env.py",
    ALEMBIC_CONFIG,
    FRONTEND_DIST / "index.html",
    POSTGRES_DIST,
)
missing_resources = [str(path) for path in required_resources if not path.exists()]
if missing_resources:
    raise SystemExit(
        "required runtime packaging resources are missing:\n- "
        + "\n- ".join(missing_resources)
    )

datas = [
    (str(MIGRATIONS), "migrations"),
    (str(ALEMBIC_CONFIG), "."),
    (str(FRONTEND_DIST), "frontend/dist"),
    (str(POSTGRES_DIST), "postgres"),
]
binaries = []
hiddenimports = []

# These packages use runtime imports, entry points, or optional native modules that
# PyInstaller cannot reliably discover from the desktop entry point alone.
for package_name in (
    "keyring",
    "sqlalchemy",
    "asyncpg",
    "uvicorn",
    "py_clob_client_v2",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="poly-runtime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="poly-runtime",
)
