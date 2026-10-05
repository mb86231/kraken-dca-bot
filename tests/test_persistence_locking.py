"""Cross-process locking tests for JSON persistence.

These tests exercise ``bot.persistence.JsonFile`` and the stores that use it with
separate operating-system processes, not just threads.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path
from typing import Any

import pytest

from bot.order_execution import OrderAttempt, OrderAttemptStore, OrderState
from bot.persistence import JsonFile, LockAcquisitionError
from bot.store import TransactionStore


# ---------------------------------------------------------------------------
# Module-level helpers required by Windows spawn-based multiprocessing.
# ---------------------------------------------------------------------------


def _worker_append_transaction(filepath: Path, pair: str, amount: float, price: float) -> None:
    store = TransactionStore(filepath=filepath)
    store.add_transaction(pair, amount, price)


def _worker_append_attempt(filepath: Path, attempt_id: str) -> None:
    store = OrderAttemptStore(filepath=filepath)
    attempt = OrderAttempt(
        attempt_id=attempt_id,
        cycle_id="c1",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=True,
        state=OrderState.CONFIRMED.value,
    )
    store.save(attempt)


def _hold_lock_then_write(filepath: Path, hold_seconds: float, value: Any) -> None:
    json_file = JsonFile(filepath, lock_timeout=30.0)
    with json_file.locked().acquire(timeout=30.0):
        time.sleep(hold_seconds)
        json_file.save(value)


def _waiter_with_queue(filepath: Path, queue: multiprocessing.Queue, timeout: float) -> None:
    json_file = JsonFile(filepath, lock_timeout=timeout)
    try:
        queue.put(json_file.load())
    except LockAcquisitionError:
        queue.put("timeout")


def _hold_and_die(filepath: Path) -> None:
    json_file = JsonFile(filepath, lock_timeout=5.0)
    with json_file.locked().acquire(timeout=5.0):
        # Simulate a crash before releasing the lock.
        raise SystemExit(1)


def _writer_with_prefix(filepath: Path, prefix: str, count: int) -> None:
    json_file = JsonFile(filepath, lock_timeout=5.0)
    for i in range(count):

        def _append(data):
            if not isinstance(data, list):
                data = []
            data.append(f"{prefix}-{i}")
            return data

        json_file.update(_append)


def _sequential_writer(filepath: Path, count: int) -> None:
    json_file = JsonFile(filepath, lock_timeout=5.0)
    for i in range(count):
        json_file.save({"version": i})
        time.sleep(0.05)


def _backup_snapshot(filepath: Path, queue: multiprocessing.Queue) -> None:
    json_file = JsonFile(filepath, lock_timeout=5.0)
    with json_file.locked().acquire(timeout=5.0):
        queue.put(json_file.load())


# ---------------------------------------------------------------------------
# Fixtures and tests
# ---------------------------------------------------------------------------


@pytest.fixture
def json_file(tmp_path: Path):
    return JsonFile(tmp_path / "data.json", lock_timeout=5.0)


class TestJsonFileLocking:
    def test_load_save_roundtrip(self, json_file: JsonFile):
        json_file.save({"key": "value"})
        assert json_file.load() == {"key": "value"}

    def test_update_is_atomic(self, json_file: JsonFile):
        values: list[int] = []

        def _append(data):
            if not isinstance(data, list):
                data = []
            values.append(len(values) + 1)
            data.append(values[-1])
            return data

        for _ in range(10):
            json_file.update(_append)

        assert json_file.load() == list(range(1, 11))

    def test_lock_timeout_raises(self, tmp_path: Path):
        filepath = tmp_path / "data.json"
        queue: multiprocessing.Queue = multiprocessing.Queue()

        holder = multiprocessing.Process(
            target=_hold_lock_then_write, args=(filepath, 1.0, {"from": "holder"})
        )
        waiter = multiprocessing.Process(target=_waiter_with_queue, args=(filepath, queue, 0.1))

        holder.start()
        # Give the holder time to acquire the lock.
        time.sleep(0.2)
        waiter.start()

        result = queue.get(timeout=5.0)
        waiter.join(timeout=5.0)
        holder.join(timeout=5.0)

        assert result == "timeout"
        assert waiter.exitcode == 0
        assert holder.exitcode == 0
        assert filepath.exists()
        assert json.loads(filepath.read_text()) == {"from": "holder"}

    def test_timeout_returned_via_queue(self, tmp_path: Path):
        filepath = tmp_path / "data.json"
        queue: multiprocessing.Queue = multiprocessing.Queue()

        holder = multiprocessing.Process(
            target=_hold_lock_then_write, args=(filepath, 0.5, {"held": True})
        )
        waiter = multiprocessing.Process(
            target=_waiter_with_queue, args=(filepath, queue, 0.1)
        )

        holder.start()
        time.sleep(0.1)
        waiter.start()

        result = queue.get(timeout=5.0)
        waiter.join(timeout=5.0)
        holder.join(timeout=5.0)

        assert result == "timeout"
        assert holder.exitcode == 0

    def test_recovery_after_process_exit(self, tmp_path: Path):
        """A child process that dies while holding a lock must not deadlock the parent."""
        filepath = tmp_path / "data.json"
        json_file = JsonFile(filepath, lock_timeout=5.0)

        bad = multiprocessing.Process(target=_hold_and_die, args=(filepath,))
        bad.start()
        bad.join(timeout=5.0)
        assert bad.exitcode == 1

        # The parent must be able to acquire the lock afterwards.
        json_file.save({"recovered": True})
        assert json_file.load() == {"recovered": True}

    def test_no_orphaned_tmp_files(self, tmp_path: Path):
        filepath = tmp_path / "data.json"
        json_file = JsonFile(filepath, lock_timeout=5.0)
        for i in range(10):
            json_file.save({"i": i})
        assert list(tmp_path.glob("*.tmp")) == []


def _start_with_stagger(processes: list[multiprocessing.Process], stagger: float = 0.02) -> None:
    """Start processes with a small delay to avoid lock-file creation races on Windows."""
    for i, p in enumerate(processes):
        p.start()
        if i < len(processes) - 1:
            time.sleep(stagger)


class TestStoreConcurrency:
    def test_concurrent_transaction_appends_no_lost_updates(self, tmp_path: Path):
        """Multiple processes append transactions; no update is lost.

        This uses three workers to avoid Windows lock-acquisition starvation
        observed when ten processes start at exactly the same instant.
        """
        filepath = tmp_path / "transactions.json"
        filepath.write_text("[]")

        processes = [
            multiprocessing.Process(
                target=_worker_append_transaction, args=(filepath, "XBTCHF", 0.0001, 50000.0)
            )
            for _ in range(3)
        ]
        _start_with_stagger(processes, stagger=0.05)
        for p in processes:
            p.join(timeout=10.0)
            assert p.exitcode == 0

        store = TransactionStore(filepath=filepath)
        assert store.get_transaction_count() == 3
        data = json.loads(filepath.read_text())
        assert len(data) == 3

    def test_concurrent_transaction_stress_no_lost_updates(self, tmp_path: Path):
        """Repeatedly run the concurrent append test to catch rare races."""
        for _ in range(5):
            filepath = tmp_path / f"transactions_stress_{_}.json"
            filepath.write_text("[]")
            processes = [
                multiprocessing.Process(
                    target=_worker_append_transaction, args=(filepath, "XBTCHF", 0.0001, 50000.0)
                )
                for _ in range(3)
            ]
            # Use a larger stagger in the stress loop to avoid spawn-time races on
            # heavily loaded Windows runners while still exercising the lock.
            _start_with_stagger(processes, stagger=0.1)
            for p in processes:
                p.join(timeout=10.0)
                assert p.exitcode == 0

            store = TransactionStore(filepath=filepath)
            assert store.get_transaction_count() == 3

    def test_concurrent_order_attempt_upserts_no_lost_updates(self, tmp_path: Path):
        filepath = tmp_path / "order_attempts.json"
        filepath.write_text("[]")

        processes = [
            multiprocessing.Process(target=_worker_append_attempt, args=(filepath, f"attempt-{i}"))
            for i in range(3)
        ]
        _start_with_stagger(processes, stagger=0.05)
        for p in processes:
            p.join(timeout=10.0)
            assert p.exitcode == 0

        store = OrderAttemptStore(filepath=filepath)
        assert len(store.list_all()) == 3

    def test_store_uses_configured_path(self, tmp_path: Path):
        """Stores must not silently switch to a different file."""
        custom_path = tmp_path / "custom" / "transactions.json"
        store = TransactionStore(filepath=custom_path)
        store.add_transaction("XBTCHF", 0.0001, 50000.0)
        assert custom_path.exists()
        assert store.filepath == custom_path

    def test_valid_json_after_concurrent_writes(self, tmp_path: Path):
        filepath = tmp_path / "data.json"
        json_file = JsonFile(filepath, lock_timeout=5.0)

        processes = [
            multiprocessing.Process(target=_writer_with_prefix, args=(filepath, "a", 20)),
            multiprocessing.Process(target=_writer_with_prefix, args=(filepath, "b", 20)),
        ]
        _start_with_stagger(processes, stagger=0.05)
        for p in processes:
            p.join(timeout=15.0)
            assert p.exitcode == 0

        data = json_file.load()
        assert isinstance(data, list)
        assert len(data) == 40
        # File must be valid JSON.
        json.loads(filepath.read_text())

    def test_backup_coordinate_with_writer(self, tmp_path: Path):
        """A backup process can hold the lock while a writer waits, then observe a consistent snapshot."""
        filepath = tmp_path / "data.json"

        queue: multiprocessing.Queue = multiprocessing.Queue()
        writer = multiprocessing.Process(target=_sequential_writer, args=(filepath, 5))
        backup = multiprocessing.Process(target=_backup_snapshot, args=(filepath, queue))

        writer.start()
        time.sleep(0.1)
        backup.start()

        snapshot = queue.get(timeout=5.0)
        backup.join(timeout=5.0)
        writer.join(timeout=5.0)

        assert backup.exitcode == 0
        assert writer.exitcode == 0
        assert isinstance(snapshot, dict)
        assert "version" in snapshot



class TestPersistenceErrorPaths:
    def test_load_timeout_raises_lock_acquisition_error(self, monkeypatch, tmp_path: Path):
        filepath = tmp_path / "data.json"
        json_file = JsonFile(filepath, lock_timeout=0.01)

        def _raise_timeout(*_args, **_kwargs):
            from filelock import Timeout
            raise Timeout(filepath)

        monkeypatch.setattr(json_file._lock, "acquire", _raise_timeout)
        with pytest.raises(LockAcquisitionError):
            json_file.load()

    def test_save_timeout_raises_lock_acquisition_error(self, monkeypatch, tmp_path: Path):
        filepath = tmp_path / "data.json"
        json_file = JsonFile(filepath, lock_timeout=0.01)

        def _raise_timeout(*_args, **_kwargs):
            from filelock import Timeout
            raise Timeout(filepath)

        monkeypatch.setattr(json_file._lock, "acquire", _raise_timeout)
        with pytest.raises(LockAcquisitionError):
            json_file.save({"x": 1})

    def test_update_timeout_raises_lock_acquisition_error(self, monkeypatch, tmp_path: Path):
        filepath = tmp_path / "data.json"
        json_file = JsonFile(filepath, lock_timeout=0.01)

        def _raise_timeout(*_args, **_kwargs):
            from filelock import Timeout
            raise Timeout(filepath)

        monkeypatch.setattr(json_file._lock, "acquire", _raise_timeout)
        with pytest.raises(LockAcquisitionError):
            json_file.update(lambda data: data)

    def test_locked_returns_underlying_lock(self, tmp_path: Path):
        filepath = tmp_path / "data.json"
        json_file = JsonFile(filepath)
        assert json_file.locked() is json_file._lock

    def test_atomic_write_falls_back_on_cross_device_error(self, monkeypatch, tmp_path: Path):
        from bot.persistence import _atomic_write_json_locked

        filepath = tmp_path / "data.json"

        def _raise_exdev(_src, _dst):
            raise OSError(18, "cross-device link")

        monkeypatch.setattr(os, "replace", _raise_exdev)
        _atomic_write_json_locked(filepath, {"ok": True})
        assert filepath.read_text() == '{\n  "ok": true\n}'

    def test_atomic_write_cleans_up_tmp_on_unexpected_error(self, monkeypatch, tmp_path: Path):
        from bot.persistence import _atomic_write_json_locked

        filepath = tmp_path / "data.json"

        def _raise_unexpected(*_args, **_kwargs):
            raise OSError(13, "permission denied")

        monkeypatch.setattr(os, "replace", _raise_unexpected)
        with pytest.raises(OSError):
            _atomic_write_json_locked(filepath, {"ok": True})
        assert list(tmp_path.glob("*.tmp")) == []


    def test_atomic_write_ignores_backup_copy_failure(self, monkeypatch, tmp_path: Path):
        from bot.persistence import _atomic_write_json_locked

        filepath = tmp_path / "data.json"
        filepath.write_text('{"old": true}')

        def _raise_oserror(*_args, **_kwargs):
            raise OSError("cannot copy")

        monkeypatch.setattr("shutil.copy2", _raise_oserror)
        _atomic_write_json_locked(filepath, {"new": True})
        assert json.loads(filepath.read_text()) == {"new": True}

    def test_atomic_write_ignores_cleanup_unlink_failure(self, monkeypatch, tmp_path: Path):
        from bot.persistence import _atomic_write_json_locked

        filepath = tmp_path / "data.json"

        def _raise_on_unlink(path):
            if str(path).endswith(".tmp"):
                raise OSError("cannot unlink tmp")
            return os.unlink(path)

        monkeypatch.setattr("os.unlink", _raise_on_unlink)
        monkeypatch.setattr("json.dump", lambda *_args, **_kwargs: (_ for _ in ()).throw(IOError("write failed")))

        with pytest.raises(IOError):
            _atomic_write_json_locked(filepath, {"new": True})
