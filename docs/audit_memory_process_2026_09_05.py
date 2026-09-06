"""Independent cross-process transaction regression; temporary local files only."""
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog


def test_outcome_commit_preserves_another_process_append(tmp_path):
    path = tmp_path / "memory.md"
    log = TradingMemoryLog({"memory_log_path": str(path)})
    log.store_decision("600519", "2026-01-05", "Rating: Buy")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    update_script = '''
import sys, time
from pathlib import Path
from tradingagents.agents.utils.memory import TradingMemoryLog
path, control = Path(sys.argv[1]), Path(sys.argv[2])
real_read = Path.read_text
def controlled_read(p, *a, **kw):
    text = real_read(p, *a, **kw)
    if p == path:
        (control / "ready").touch()
        deadline = time.monotonic() + 10
        while not (control / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("audit parent failed to release updater")
            time.sleep(.01)
    return text
Path.read_text = controlled_read
TradingMemoryLog({"memory_log_path": str(path)}).batch_update_with_outcomes([
    dict(ticker="600519", trade_date="2026-01-05", raw_return=.1,
         alpha_return=.05, holding_days=5, reflection="offline", outcome_end="2026-01-12")])
'''
    append_script = '''
import sys
from pathlib import Path
from tradingagents.agents.utils.memory import TradingMemoryLog
Path(sys.argv[2], "b_ready").touch()
TradingMemoryLog({"memory_log_path": sys.argv[1]}).store_decision("000001", "2026-01-06", "Rating: Sell")
'''
    a = subprocess.Popen([sys.executable, "-c", update_script, str(path), str(tmp_path)],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    b = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists():
            assert time.monotonic() < deadline, "Updater failed to reach transaction read"
            time.sleep(.01)
        b = subprocess.Popen([sys.executable, "-c", append_script, str(path), str(tmp_path)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while not (tmp_path / "b_ready").exists():
            assert time.monotonic() < deadline, "Appender failed to start"
            time.sleep(.01)
        # An unlocked writer can finish here. A correctly locked writer must
        # wait until release; either way this controlled order must preserve it.
        time.sleep(.3)
        (tmp_path / "release").touch()
        for process in (a, b):
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr.decode()
    finally:
        (tmp_path / "release").touch()
        for process in (a, b):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
    entries = log.load_entries()
    assert {entry["ticker"] for entry in entries} == {"600519", "000001"}
    assert sum(not entry["pending"] for entry in entries) == 1


def test_lock_open_failure_must_not_enter_unprotected_transaction(monkeypatch, tmp_path):
    from tradingagents.agents.utils import file_lock as locking
    monkeypatch.setattr(locking.os, "open", Mock(side_effect=PermissionError(13, "simulated lock denial")))
    with pytest.raises(PermissionError):
        with locking.file_lock(tmp_path / "memory.md"):
            pytest.fail("Transaction was permitted without the required OS lock")


def test_symlink_alias_cannot_acquire_an_independent_lock(tmp_path):
    from tradingagents.agents.utils.file_lock import file_lock
    target, alias = tmp_path / "real.md", tmp_path / "alias.md"
    target.touch()
    alias.symlink_to(target)
    with file_lock(target):
        with pytest.raises(TimeoutError):
            with file_lock(alias, timeout=.02):
                pytest.fail("The same physical file was protected by two different locks")


def test_memory_updates_follow_symlink_without_replacing_alias(tmp_path):
    target, alias = tmp_path / "real.md", tmp_path / "alias.md"
    original = TradingMemoryLog({"memory_log_path": str(target)})
    original.store_decision("600519", "2026-01-05", "Rating: Buy")
    alias.symlink_to(target)
    other = TradingMemoryLog({"memory_log_path": str(alias)})
    other.update_with_outcome("600519", "2026-01-05", .1, .05, 5, "offline", outcome_end="2026-01-12")
    assert alias.is_symlink(), "Atomic replacement detached the alias from the actual log"
    assert not original.get_pending_entries(), "Update failed to reach the shared log"
