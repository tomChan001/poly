from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.desktop import parent


def packaged_layout(tmp_path: Path, team: str) -> tuple[Path, Path, Path]:
    contents = tmp_path / "Poly.app" / "Contents"
    root = contents / "Resources" / "poly-runtime" / "_internal"
    native = contents / "MacOS" / "Poly"
    runtime = root.parent / "poly-runtime"
    for path in (native, runtime):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"signed")
    (root / parent.EXPECTED_IDENTITY_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / parent.EXPECTED_IDENTITY_FILE).write_text(team + "\n", encoding="ascii")
    return root, native.resolve(), runtime.resolve()


@pytest.mark.parametrize(
    ("expected_team", "signature", "accepted"),
    [
        (
            "ABCDE12345",
            parent.CodeSignature("com.poly.desktop", "ABCDE12345", False),
            True,
        ),
        (
            "ABCDE12345",
            parent.CodeSignature("com.poly.desktop", "OTHER12345", False),
            False,
        ),
        (
            parent.ADHOC_VALIDATION_TEAM,
            parent.CodeSignature("com.poly.desktop", None, True),
            True,
        ),
    ],
)
def test_frozen_runtime_authenticates_exact_parent_and_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_team: str,
    signature: parent.CodeSignature,
    accepted: bool,
) -> None:
    root, native, _runtime = packaged_layout(tmp_path, expected_team)
    monkeypatch.setattr(parent.sys, "frozen", True, raising=False)
    monkeypatch.setattr(parent.sys, "platform", "darwin")
    monkeypatch.setattr(parent, "_parent_executable", lambda _pid: native)
    verified: list[tuple[Path, bool]] = []

    def verify(path: Path, *, deep: bool = False) -> parent.CodeSignature:
        verified.append((path, deep))
        return signature

    monkeypatch.setattr(parent, "_verified_signature", verify)
    dynamic_requirements: list[str | None] = []

    def verify_running(
        _pid: int, *, requirement: str | None
    ) -> parent.CodeSignature:
        dynamic_requirements.append(requirement)
        return signature

    monkeypatch.setattr(parent, "_verified_running_signature", verify_running)

    assert parent.trusted_packaged_parent(root) is accepted
    assert verified == [
        (root.parents[3], True),
        (root.parent / "poly-runtime", False),
    ]
    if expected_team == parent.ADHOC_VALIDATION_TEAM:
        assert dynamic_requirements == [None]
    else:
        assert len(dynamic_requirements) == 1
        requirement = dynamic_requirements[0]
        assert requirement is not None
        assert 'anchor apple generic' in requirement
        assert 'identifier "com.poly.desktop"' in requirement
        assert 'field.1.2.840.113635.100.6.1.13' in requirement
        assert f'subject.OU] = "{expected_team}"' in requirement


def test_frozen_runtime_rejects_a_valid_signature_from_the_wrong_parent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _native, _runtime = packaged_layout(tmp_path, "ABCDE12345")
    wrong_parent = tmp_path / "attacker" / "Poly"
    wrong_parent.parent.mkdir()
    wrong_parent.write_bytes(b"signed")
    monkeypatch.setattr(parent.sys, "frozen", True, raising=False)
    monkeypatch.setattr(parent.sys, "platform", "darwin")
    monkeypatch.setattr(
        parent, "_parent_executable", lambda _pid: wrong_parent.resolve()
    )
    monkeypatch.setattr(
        parent,
        "_verified_signature",
        lambda _path, **_kwargs: parent.CodeSignature(
            "com.poly.desktop", "ABCDE12345", False
        ),
    )
    monkeypatch.setattr(
        parent,
        "_verified_running_signature",
        lambda _pid, **_kwargs: parent.CodeSignature(
            "com.poly.desktop", "ABCDE12345", False
        ),
    )

    assert parent.trusted_packaged_parent(root) is False


def test_frozen_runtime_rejects_parent_change_after_dynamic_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, native, _runtime = packaged_layout(tmp_path, "ABCDE12345")
    signature = parent.CodeSignature("com.poly.desktop", "ABCDE12345", False)
    monkeypatch.setattr(parent.sys, "frozen", True, raising=False)
    monkeypatch.setattr(parent.sys, "platform", "darwin")
    parent_pids = iter((700, 1))
    monkeypatch.setattr(parent.os, "getppid", lambda: next(parent_pids))
    monkeypatch.setattr(parent, "_parent_executable", lambda _pid: native)
    monkeypatch.setattr(parent, "_verified_signature", lambda _path, **_kw: signature)
    monkeypatch.setattr(
        parent, "_verified_running_signature", lambda _pid, **_kw: signature
    )

    assert parent.trusted_packaged_parent(root) is False


def test_frozen_runtime_rejects_parent_path_change_after_dynamic_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, native, _runtime = packaged_layout(tmp_path, "ABCDE12345")
    wrong_parent = tmp_path / "replaced" / "Poly"
    wrong_parent.parent.mkdir()
    wrong_parent.write_bytes(b"replacement")
    parent_paths = iter((native, wrong_parent.resolve()))
    signature = parent.CodeSignature("com.poly.desktop", "ABCDE12345", False)
    monkeypatch.setattr(parent.sys, "frozen", True, raising=False)
    monkeypatch.setattr(parent.sys, "platform", "darwin")
    monkeypatch.setattr(parent.os, "getppid", lambda: 700)
    monkeypatch.setattr(
        parent, "_parent_executable", lambda _pid: next(parent_paths)
    )
    monkeypatch.setattr(parent, "_verified_signature", lambda _path, **_kw: signature)
    monkeypatch.setattr(
        parent, "_verified_running_signature", lambda _pid, **_kw: signature
    )

    assert parent.trusted_packaged_parent(root) is False


def test_dynamic_parent_verification_uses_pid_and_release_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        if "--display" in arguments:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=(
                    "Identifier=com.poly.desktop\n"
                    "TeamIdentifier=ABCDE12345\n"
                    "Signature=Developer ID Application\n"
                ),
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(parent.subprocess, "run", run)
    requirement = (
        '=anchor apple generic and identifier "com.poly.desktop" '
        "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists "
        'and certificate leaf[subject.OU] = "ABCDE12345"'
    )

    signature = parent._verified_running_signature(4242, requirement=requirement)

    assert signature == parent.CodeSignature(
        "com.poly.desktop", "ABCDE12345", False
    )
    assert calls[0][-1] == "4242"
    assert calls[0][-2] == f"-R={requirement}"
    assert calls[1][-1] == "4242"


def test_dynamic_parent_verification_fails_closed_on_codesign_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        parent.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="invalid"
        ),
    )

    with pytest.raises(RuntimeError, match="running code signature"):
        parent._verified_running_signature(4242, requirement=None)


def test_non_frozen_development_runtime_does_not_require_codesign(tmp_path: Path) -> None:
    assert parent.trusted_packaged_parent(tmp_path) is True
