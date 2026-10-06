import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from contrib import capture_keyboard_trial as capture


@pytest.mark.parametrize("timeout", [False, True])
def test_capture_detaches_without_signalling_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: bool) -> None:
    (tmp_path / "metadata.json").write_text(json.dumps({"worker_pid": 1234, "descriptors": [5, 22]}))
    tracer = Mock(pid=5678, returncode=0)
    if timeout:
        tracer.communicate.side_effect = [subprocess.TimeoutExpired("strace", 90), None]
    spawn = Mock(return_value=tracer)
    monkeypatch.setattr(capture.subprocess, "Popen", spawn)
    monkeypatch.setattr(capture.signal, "signal", Mock())
    capture.record(tmp_path)
    command = spawn.call_args.args[0]
    assert command[command.index("-p") + 1] == "1234"
    assert "trace-fds=5,22" in command
    assert "trace=read,write,writev" in command
    if timeout:
        tracer.send_signal.assert_called_once_with(signal.SIGINT)
    else:
        tracer.send_signal.assert_not_called()
    tracer.kill.assert_not_called()
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert metadata["tracer_returncode"] == 0
    assert metadata["ended_at"] >= metadata["started_at"]


def test_capture_kills_only_unresponsive_tracer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "metadata.json").write_text(json.dumps({"worker_pid": 1234, "descriptors": [5, 22]}))
    tracer = Mock(pid=5678, returncode=-9)
    tracer.communicate.side_effect = [subprocess.TimeoutExpired("strace", 90), subprocess.TimeoutExpired("strace", 5), None]
    monkeypatch.setattr(capture.subprocess, "Popen", Mock(return_value=tracer))
    monkeypatch.setattr(capture.signal, "signal", Mock())
    capture.record(tmp_path)
    tracer.send_signal.assert_called_once_with(signal.SIGINT)
    tracer.kill.assert_called_once_with()
