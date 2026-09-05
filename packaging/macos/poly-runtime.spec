# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


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
PARENT_IDENTITY_FILE = Path(os.environ["POLY_PARENT_IDENTITY_FILE"]).resolve()

required_resources = (
    ENTRY_POINT,
    MIGRATIONS / "env.py",
    ALEMBIC_CONFIG,
    FRONTEND_DIST / "index.html",
    POSTGRES_DIST,
    PARENT_IDENTITY_FILE,
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
    (str(PARENT_IDENTITY_FILE), "."),
]
datas.extend(copy_metadata("keyring"))
binaries = []

# Only modules reached through entry points, extension-module imports, or string
# configuration need to be named here. Ordinary Python imports remain discoverable
# by Analysis and must not be duplicated as data files.
REQUIRED_HIDDENIMPORTS = (
    "keyring.backends.chainer",
    "keyring.backends.fail",
    "keyring.backends.macOS",
    "keyring.backends.macOS.api",
    "sqlalchemy.dialects.postgresql.asyncpg",
    "asyncpg.pgproto.pgproto",
    "asyncpg.protocol.protocol",
    "asyncpg.protocol.record",
    "uvicorn.loops.auto",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_sansio_impl",
    "uvicorn.lifespan.on",
    "py_clob_client_v2.client",
    "py_clob_client_v2.clob_types",
)
FORBIDDEN_HIDDENIMPORT_PARTS = (".testing", ".tests", "_testbase")
hiddenimports = sorted(set(REQUIRED_HIDDENIMPORTS))
missing_hiddenimports = sorted(set(REQUIRED_HIDDENIMPORTS) - set(hiddenimports))
forbidden_hiddenimports = [
    name
    for name in hiddenimports
    if any(part in name for part in FORBIDDEN_HIDDENIMPORT_PARTS)
]
if missing_hiddenimports or forbidden_hiddenimports:
    raise SystemExit(
        "invalid targeted hidden imports: "
        f"missing={missing_hiddenimports}, forbidden={forbidden_hiddenimports}"
    )

EXCLUDED_IMPORTS = (
    "asyncpg._testbase",
    "keyring.testing",
    "keyring.backends.kwallet",
    "keyring.backends.libsecret",
    "keyring.backends.null",
    "keyring.backends.SecretService",
    "keyring.backends.Windows",
    "sqlalchemy.testing",
    "sqlalchemy.dialects.mssql",
    "sqlalchemy.dialects.mysql",
    "sqlalchemy.dialects.oracle",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.ext.baked",
    "MySQLdb",
    "psycopg2",
    "pysqlite2",
    "uvicorn.__main__",
    "uvicorn.lifespan.off",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.zttp_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.supervisors.statreload",
    "uvicorn.workers",
)

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(EXCLUDED_IMPORTS),
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
