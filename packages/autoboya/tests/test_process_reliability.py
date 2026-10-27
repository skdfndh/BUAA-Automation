import os
import signal
import subprocess
import sys
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from autoboya import cli
from autoboya.config import RUN_LOCK_FILE, RUN_PID_FILE
from autoboya.exceptions import OperationInProgress
from autoboya.scheduler import AutomationRunner
from autoboya.storage import AutoBoyaStore


def test_file_lock_excludes_second_process_and_releases(tmp_path):
    store = AutoBoyaStore(tmp_path)
    script = """
import sys
from pathlib import Path
from autoboya.storage import AutoBoyaStore
from autoboya.exceptions import OperationInProgress
try:
    with AutoBoyaStore(Path(sys.argv[1])).lock('run/autoboya.lock'):
        print('acquired')
except OperationInProgress:
    print('blocked')
"""
    def attempt():
        return subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=True, timeout=10).stdout.strip()

    with store.lock(RUN_LOCK_FILE):
        assert attempt() == "blocked"
    assert attempt() == "acquired"


def test_real_worker_refuses_duplicate_start_and_stops(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOBOYA_HOME", str(tmp_path))
    store = AutoBoyaStore(tmp_path)
    process = cli.start_background_worker(store)
    try:
        assert int(store.path(RUN_PID_FILE).read_text().strip()) > 0
        with pytest.raises(OperationInProgress):
            cli.start_background_worker(store)
        with pytest.raises(OperationInProgress):
            AutomationRunner(store).run_once()
        AutomationRunner(store).request_stop()
        assert process.wait(timeout=10) == 0
        assert not store.path(RUN_PID_FILE).exists()
        with store.lock(RUN_LOCK_FILE):
            pass
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_worker_crash_releases_lock_and_stale_pid_does_not_block_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOBOYA_HOME", str(tmp_path))
    store = AutoBoyaStore(tmp_path)
    process = cli.start_background_worker(store)
    os.kill(int(store.path(RUN_PID_FILE).read_text().strip()), signal.SIGTERM)
    process.wait(timeout=5)
    assert store.path(RUN_PID_FILE).exists()
    replacement = cli.start_background_worker(store)
    try:
        assert replacement.pid != process.pid
        AutomationRunner(store).request_stop()
        assert replacement.wait(timeout=10) == 0
    finally:
        if replacement.poll() is None:
            replacement.terminate()
            replacement.wait(timeout=5)


def test_failed_worker_start_is_reported(monkeypatch, tmp_path):
    process = Mock(pid=9999)
    process.poll.return_value = 1
    monkeypatch.setattr(cli, "spawn_background_worker", lambda store: process)
    with pytest.raises(RuntimeError, match="启动失败"):
        cli.start_background_worker(AutoBoyaStore(tmp_path))


def test_worker_start_timeout_terminates_only_new_process(monkeypatch, tmp_path):
    process = Mock(pid=9999)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("worker", 5), 0]
    ticks = iter([0, 11])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cli, "spawn_background_worker", lambda store: process)
    with pytest.raises(RuntimeError, match="未能及时启动"):
        cli.start_background_worker(AutoBoyaStore(tmp_path))
    process.terminate.assert_called_once()
    assert process.wait.call_count == 2


def test_cli_duplicate_start_has_no_traceback(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOBOYA_HOME", str(tmp_path))
    store = AutoBoyaStore(tmp_path)
    with store.lock(RUN_LOCK_FILE):
        result = CliRunner().invoke(cli.app, ["run"])
    assert result.exit_code == 1
    assert "正在运行" in result.output
    assert "Traceback" not in result.output


def test_spawn_passes_explicit_data_directory_to_child(monkeypatch, tmp_path):
    store = AutoBoyaStore(tmp_path / "custom-data")
    store.init()
    popen = Mock()
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    cli.spawn_background_worker(store)
    assert popen.call_args.kwargs["env"]["AUTOBOYA_HOME"] == str(store.root.resolve())
