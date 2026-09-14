import runpy
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / 'run-installed-smoke.py'


@pytest.mark.parametrize('needs_kill', [False, True])
def test_timeout_allows_group_cleanup_before_force_kill(monkeypatch, needs_kill):
    module = runpy.run_path(str(HELPER))
    calls = []

    class Process:
        pid = 12345

        def wait(self, *, timeout):
            calls.append(('wait', timeout))
            if timeout == 600 or (timeout == 30 and needs_kill):
                raise subprocess.TimeoutExpired('synthetic-smoke', timeout)
            return 0

    def popen(args, **kwargs):
        assert kwargs['start_new_session'] is True
        return Process()

    monkeypatch.setattr(module['subprocess'], 'Popen', popen)
    monkeypatch.setattr(module['os'], 'killpg', lambda pid, sig: calls.append(('signal', sig)), raising=False)
    monkeypatch.setattr(module['signal'], 'SIGKILL', 9, raising=False)
    assert module['run_script']('true', Path('.')) == 124
    expected = [('wait', 600), ('signal', module['signal'].SIGTERM), ('wait', 30)]
    if needs_kill:
        expected.extend([('signal', 9), ('wait', 5)])
    assert calls == expected
