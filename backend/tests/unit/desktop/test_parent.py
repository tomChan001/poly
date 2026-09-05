from pathlib import Path

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

    assert parent.trusted_packaged_parent(root) is accepted
    assert verified == [
        (root.parents[3], True),
        (root.parent / "poly-runtime", False),
    ]


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

    assert parent.trusted_packaged_parent(root) is False


def test_non_frozen_development_runtime_does_not_require_codesign(tmp_path: Path) -> None:
    assert parent.trusted_packaged_parent(tmp_path) is True
