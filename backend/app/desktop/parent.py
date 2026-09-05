from __future__ import annotations

import ctypes
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ADHOC_VALIDATION_TEAM = "ADHOC_VALIDATION_ONLY"
EXPECTED_IDENTITY_FILE = "expected-parent-team-id"
_TEAM_ID = re.compile(r"[A-Z0-9]{10}\Z")


@dataclass(frozen=True, slots=True)
class CodeSignature:
    identifier: str
    team_identifier: str | None
    ad_hoc: bool


def trusted_packaged_parent(project_root: Path) -> bool:
    """Authenticate the native parent of a frozen runtime without trusting argv."""
    if not getattr(sys, "frozen", False):
        return True
    if sys.platform != "darwin":
        return False
    try:
        root = project_root.resolve(strict=True)
        identity_file = root / EXPECTED_IDENTITY_FILE
        identity_metadata = identity_file.stat(follow_symlinks=False)
        if (
            identity_file.is_symlink()
            or not stat.S_ISREG(identity_metadata.st_mode)
            or identity_metadata.st_nlink != 1
            or not identity_file.resolve(strict=True).is_relative_to(root)
        ):
            return False
        expected_team = identity_file.read_text(encoding="ascii").strip()
        if expected_team != ADHOC_VALIDATION_TEAM and not _TEAM_ID.fullmatch(
            expected_team
        ):
            return False

        contents = root.parents[2]
        app_bundle = contents.parent
        if contents.name != "Contents" or app_bundle.name != "Poly.app":
            return False
        expected_parent = (contents / "MacOS" / "Poly").resolve(strict=True)
        runtime_executable = (root.parent / "poly-runtime").resolve(strict=True)
        if _parent_executable(os.getppid()) != expected_parent:
            return False

        # Verify the complete bundle so the sealed identity resource cannot be
        # replaced and used to downgrade a production build to ad-hoc mode.
        parent_signature = _verified_signature(app_bundle, deep=True)
        runtime_signature = _verified_signature(runtime_executable)
        if parent_signature.identifier != "com.poly.desktop":
            return False
        if expected_team == ADHOC_VALIDATION_TEAM:
            return bool(
                parent_signature.ad_hoc
                and runtime_signature.ad_hoc
                and parent_signature.team_identifier is None
                and runtime_signature.team_identifier is None
            )
        return bool(
            not parent_signature.ad_hoc
            and not runtime_signature.ad_hoc
            and parent_signature.team_identifier == expected_team
            and runtime_signature.team_identifier == expected_team
        )
    except (IndexError, OSError, RuntimeError, UnicodeError, ValueError):
        return False


def _parent_executable(pid: int) -> Path:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidpath = library.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    length = proc_pidpath(pid, buffer, len(buffer))
    if length <= 0:
        raise OSError(ctypes.get_errno(), "could not resolve parent executable")
    return Path(os.fsdecode(buffer.value)).resolve(strict=True)


def _verified_signature(path: Path, *, deep: bool = False) -> CodeSignature:
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    verify_arguments = ["/usr/bin/codesign", "--verify"]
    if deep:
        verify_arguments.append("--deep")
    verify_arguments.extend(["--strict", "--verbose=2", str(path)])
    verify = subprocess.run(
        verify_arguments,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=environment,
    )
    if verify.returncode != 0:
        raise RuntimeError("code signature verification failed")
    describe = subprocess.run(
        ["/usr/bin/codesign", "--display", "--verbose=4", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=environment,
    )
    if describe.returncode != 0:
        raise RuntimeError("code signature description failed")
    fields: dict[str, str] = {}
    for line in (describe.stdout + "\n" + describe.stderr).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"Identifier", "TeamIdentifier", "Signature"}:
            fields[key] = value.strip()
    identifier = fields.get("Identifier", "")
    if not identifier:
        raise RuntimeError("code signature identifier is missing")
    team = fields.get("TeamIdentifier")
    if team in {None, "", "not set"}:
        team = None
    return CodeSignature(identifier, team, fields.get("Signature") == "adhoc")
