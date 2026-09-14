"""Run the exact build-job installed acceptance against a verified CI artifact."""
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path


def smoke_script(workflow: str) -> str:
    header = '      - name: Install DMG into an isolated home and smoke test\n'
    if workflow.count(header) != 1:
        raise ValueError('installed acceptance step must be unique')
    block = workflow.split(header, 1)[1].split('\n      - name:', 1)[0]
    script = textwrap.dedent(block.split('        run: |\n', 1)[1])
    if '${{' in script:
        raise ValueError('installed acceptance must use environment variables')
    return script


def stop_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run_script(script: str, root: Path) -> int:
    process = subprocess.Popen(
        ['/bin/bash', '-e', '-c', script], cwd=root, start_new_session=True,
    )
    try:
        return process.wait(timeout=600)
    except subprocess.TimeoutExpired:
        stop_group(process)
        return 124
    except BaseException:
        stop_group(process)
        raise


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def main() -> int:
    if sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('requires hosted macOS CI')
    runner = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    dmg = Path(os.environ['POLY_EXISTING_DMG']).resolve(strict=True)
    if not dmg.is_relative_to(runner) or dmg.suffix != '.dmg' or not dmg.is_file():
        raise ValueError('requires a downloaded disposable CI artifact')
    root = Path(__file__).resolve().parents[2]
    script = smoke_script((root / '.github/workflows/macos-desktop.yml').read_text())
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return run_script(script, root)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
