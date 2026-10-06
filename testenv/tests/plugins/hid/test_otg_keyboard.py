import asyncio
import errno
import logging
import multiprocessing
import queue
import threading
from collections import deque
from collections.abc import Iterable
from typing import Any
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from evdev import ecodes

from kvmd import aiomulti
from kvmd.keyboard.mappings import KEYMAP

with patch("kvmd.utils.get_model_name", return_value="rm10"):
    from kvmd.plugins.hid.otg import device
    from kvmd.plugins.hid.otg.keyboard import KeyboardProcess
    from kvmd.plugins.hid.otg.events import BaseEvent
    from kvmd.plugins.hid.otg.events import ClearEvent
    from kvmd.plugins.hid.otg.events import ResetEvent
    from kvmd.plugins.hid.otg.events import make_keyboard_event


def _run_keyboard(
    monkeypatch: pytest.MonkeyPatch,
    events: Iterable[BaseEvent | None],
    outcomes: Iterable[bool | OSError],
    ready: Iterable[bool]=(),
    stop_after: int=0,
    clear_after: int=0,
    delayed_old_event: bool=False,
    clear_on_read: int=0,
    read_error_at: int=0,
) -> tuple[list[bytes], list[bytes], dict[str, Any]]:
    notifier = aiomulti.AioProcessNotifier()
    monkeypatch.setattr(notifier, "notify", lambda mask=0: None)
    process = KeyboardProcess(
        notifier=notifier,
        device_path="/dev/test-keyboard",
        select_timeout=0.01,
        queue_timeout=0.01,
        write_retries=3,
        noop=False,
    )
    pending = deque(None if event is None else (0, event) for event in events)
    results = deque(outcomes)
    readiness = deque(ready)
    accepted: list[bytes] = []
    attempts: list[bytes] = []
    stop = process._BaseDeviceProcess__stop_event
    idle_polls = 0
    reads = 0

    def get_event(timeout: float) -> tuple[int, BaseEvent]:
        nonlocal idle_polls
        _ = timeout
        if pending:
            event = pending.popleft()
            if event is not None:
                return event
        idle_polls += 1
        if idle_polls > 10:
            stop.set()
        raise queue.Empty

    def get_nowait() -> tuple[int, BaseEvent] | None:
        if pending:
            return pending.popleft()
        raise queue.Empty

    original_write = device.os.write

    def write_report(descriptor: int, report: bytes) -> int:
        if descriptor != 123:
            return original_write(descriptor, report)
        attempts.append(report)
        result = results.popleft() if results else True
        if stop_after and len(attempts) >= stop_after:
            stop.set()
        if clear_after and len(attempts) == clear_after:
            process.send_clear_event()
            if delayed_old_event:
                pending.append((0, make_keyboard_event(ecodes.KEY_B, True)))
        if isinstance(result, OSError):
            raise result
        if not result:
            return 0
        accepted.append(report)
        return len(report)

    def ensure_device() -> bool:
        result = readiness.popleft() if readiness else True
        if not result:
            process._BaseDeviceProcess__state_flags.update(online=False)
        return result

    def wait_for_stop(timeout: float) -> bool:
        _ = timeout
        return stop.is_set()

    def read_reports() -> None:
        nonlocal reads
        reads += 1
        if reads == clear_on_read:
            process.send_clear_event()
        if reads == read_error_at or read_error_at == -1:
            process._process_read_report(b"")

    event_queue = process._BaseDeviceProcess__events_queue
    monkeypatch.setattr(process, "_BaseDeviceProcess__events_queue", Mock(
        get=get_event, get_nowait=get_nowait, put_nowait=pending.append,
    ))
    monkeypatch.setattr(process, "_BaseDeviceProcess__fd", 123)
    monkeypatch.setattr(process, "_BaseDeviceProcess__ensure_device", ensure_device)
    monkeypatch.setattr(process, "_BaseDeviceProcess__read_all_reports", read_reports)
    monkeypatch.setattr(process, "_BaseDeviceProcess__is_udc_configured", lambda: True)
    monkeypatch.setattr(device.os, "write", write_report)
    monkeypatch.setattr(stop, "wait", wait_for_stop)
    monkeypatch.setattr(process, "_BaseDeviceProcess__close_device", lambda: None)
    monkeypatch.setattr(device.aioproc, "settle", lambda *args: logging.getLogger("test-keyboard"))
    try:
        process.run()
    finally:
        event_queue.close()
        event_queue.join_thread()
        notifier._AioProcessNotifier__queue.close()
        notifier._AioProcessNotifier__queue.join_thread()
    return accepted, attempts, asyncio.run(process.get_state())


def _report(*codes: int, modifiers: int=0) -> bytes:
    return bytes([modifiers, 0, *codes, *([0] * (6 - len(codes)))])


@pytest.mark.parametrize("outcomes", [[True, True], [False, True, True]])
def test_press_and_release_are_written_in_order(monkeypatch: pytest.MonkeyPatch, outcomes: list[bool]) -> None:
    key = KEYMAP[ecodes.KEY_A]
    events = [
        make_keyboard_event(ecodes.KEY_A, True),
        make_keyboard_event(ecodes.KEY_A, False),
    ]
    accepted, _, state = _run_keyboard(monkeypatch, events, outcomes)
    assert accepted == [bytes([0, 0, key.usb.code, 0, 0, 0, 0, 0]), bytes(8)]
    assert state["online"]


@pytest.mark.parametrize("error", [11, 108])
def test_usb_busy_errors_preserve_press(monkeypatch: pytest.MonkeyPatch, error: int) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False)]
    accepted, attempts, state = _run_keyboard(monkeypatch, events, [OSError(error, "USB busy"), True, True])
    assert attempts == [_report(4), _report(4), _report()]
    assert accepted == [_report(4), _report()]
    assert state["online"]


def test_device_not_ready_preserves_press(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False)]
    accepted, _, state = _run_keyboard(monkeypatch, events, [], ready=[False, False, False, True])
    assert accepted == [_report(4), _report()]
    assert state["online"]


def test_idle_does_not_release_held_key(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, True), None, None, None, make_keyboard_event(ecodes.KEY_A, False)]
    accepted, attempts, _ = _run_keyboard(monkeypatch, events, [])
    assert accepted == attempts == [_report(4), _report()]


def test_overlapping_keys_and_modifier_keep_order(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [make_keyboard_event(key, state) for key, state in [
        (ecodes.KEY_LEFTSHIFT, True), (ecodes.KEY_A, True), (ecodes.KEY_SPACE, True),
        (ecodes.KEY_A, False), (ecodes.KEY_SPACE, False), (ecodes.KEY_LEFTSHIFT, False),
    ]]
    accepted, _, _ = _run_keyboard(monkeypatch, events, [True, False, True])
    assert accepted == [
        _report(modifiers=2), _report(4, modifiers=2), _report(4, 44, modifiers=2),
        _report(0, 44, modifiers=2), _report(modifiers=2), _report(),
    ]


def test_every_report_of_same_key_repress_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, state) for state in [True, True, False]]
    accepted, attempts, _ = _run_keyboard(monkeypatch, events, [True, False, True, True, True])
    assert attempts == [_report(4), _report(), _report(), _report(4), _report()]
    assert accepted == [_report(4), _report(), _report(4), _report()]


def test_exhaustion_releases_keys_and_blocks_later_input(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    events = [make_keyboard_event(key, state) for key, state in [
        (ecodes.KEY_LEFTSHIFT, True), (ecodes.KEY_A, True), (ecodes.KEY_B, True),
        (ecodes.KEY_A, False), (ecodes.KEY_B, False), (ecodes.KEY_LEFTSHIFT, False),
    ]]
    accepted, _, state = _run_keyboard(monkeypatch, events, [True, False, False, False, False, False, True])
    assert accepted == [_report(modifiers=2), _report()]
    assert not state["online"]
    assert "input suspended until clear/reset" in caplog.text


@pytest.mark.parametrize("recovery", [ClearEvent(), ResetEvent()])
def test_explicit_recovery_does_not_replay_old_keys(monkeypatch: pytest.MonkeyPatch, recovery: BaseEvent) -> None:
    events = [
        make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False),
        make_keyboard_event(ecodes.KEY_B, True), recovery,
        make_keyboard_event(ecodes.KEY_C, True), make_keyboard_event(ecodes.KEY_C, False),
    ]
    accepted, _, state = _run_keyboard(monkeypatch, events, [False] * 4)
    assert accepted == [_report(), _report(), _report(6), _report()]
    assert state["online"]


def test_stop_interrupts_failed_write(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False)]
    accepted, attempts, _ = _run_keyboard(monkeypatch, events, [False], stop_after=1)
    assert accepted == []
    assert attempts == [_report(4)]


def test_release_is_retried_when_usb_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False),
        None, make_keyboard_event(ecodes.KEY_B, True), make_keyboard_event(ecodes.KEY_B, False),
    ]
    accepted, attempts, state = _run_keyboard(monkeypatch, events, [], ready=[True, True] + [False] * 6 + [True])
    assert accepted == attempts == [_report(4), _report()]
    assert not state["online"]


def test_clear_cannot_resume_input_while_usb_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        make_keyboard_event(ecodes.KEY_A, True), ClearEvent(),
        make_keyboard_event(ecodes.KEY_B, True), make_keyboard_event(ecodes.KEY_B, False),
    ]
    accepted, attempts, state = _run_keyboard(monkeypatch, events, [], ready=[False] * 10)
    assert accepted == attempts == [_report()]
    assert not state["online"]


@pytest.mark.parametrize("delayed_old_event", [False, True])
def test_clear_cancels_failed_pending_press(monkeypatch: pytest.MonkeyPatch, delayed_old_event: bool) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False)]
    accepted, attempts, state = _run_keyboard(monkeypatch, events, [False], clear_after=1, delayed_old_event=delayed_old_event)
    assert attempts == [_report(4), _report(), _report()]
    assert accepted == [_report(), _report()]
    assert state["online"]


def test_100000_transitions_with_intermittent_write_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    codes = [ecodes.KEY_A, ecodes.KEY_SPACE, ecodes.KEY_B, ecodes.KEY_ENTER]
    events = []
    expected = []
    for index in range(50000):
        code = codes[index % len(codes)]
        events.extend([make_keyboard_event(code, True), make_keyboard_event(code, False)])
        expected.extend([_report(KEYMAP[code].usb.code), _report()])
    outcomes = []
    for index in range(len(expected)):
        if index % 97 == 0:
            outcomes.append(False)
        outcomes.append(True)
    accepted, _, state = _run_keyboard(monkeypatch, events, outcomes)
    assert len(accepted) == 100000
    assert accepted == expected
    assert state["online"]


def test_clear_during_led_read_cancels_pending_press(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False)]
    accepted, attempts, state = _run_keyboard(monkeypatch, events, [], clear_on_read=2)
    assert accepted == attempts == [_report(), _report()]
    assert state["online"]


@pytest.mark.parametrize("read_error_at", [1, 2, 3, -1])
def test_led_errors_do_not_drop_key_transitions(monkeypatch: pytest.MonkeyPatch, read_error_at: int) -> None:
    events = [make_keyboard_event(ecodes.KEY_A, True), make_keyboard_event(ecodes.KEY_A, False)]
    accepted, attempts, state = _run_keyboard(monkeypatch, events, [], read_error_at=read_error_at)
    assert accepted == attempts == [_report(4), _report()]
    assert state["online"]


def _run_child_keyboard(process: KeyboardProcess, checkpoint: Any, resume: Any, released: Any, reports: Any, pause_at: str) -> None:
    reads = 0
    original_write = device.os.write

    def read_reports() -> None:
        nonlocal reads
        reads += 1
        if pause_at == "read" and reads == 2:
            checkpoint.set()
            if not resume.wait(5):
                raise RuntimeError("LED read checkpoint timed out")

    def write_report(descriptor: int, report: bytes) -> int:
        if descriptor != 123:
            return original_write(descriptor, report)
        if pause_at == "write" and report == _report(4):
            checkpoint.set()
            if not resume.wait(5):
                raise RuntimeError("Write checkpoint timed out")
        reports.put((process._BaseDeviceProcess__reset_generation.value, report))
        if report == _report():
            released.set()
        return len(report)

    with (
        patch.object(process, "_BaseDeviceProcess__fd", 123),
        patch.object(process, "_BaseDeviceProcess__ensure_device", return_value=True),
        patch.object(process, "_BaseDeviceProcess__read_all_reports", side_effect=read_reports),
        patch.object(device.os, "write", side_effect=write_report),
        patch.object(process, "_BaseDeviceProcess__close_device"),
        patch.object(process, "_BaseDeviceProcess__is_udc_configured", return_value=True),
        patch.object(device.aioproc, "settle", return_value=logging.getLogger("test-child-keyboard")),
    ):
        process.run()


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="Device uses Linux fork workers")
@pytest.mark.parametrize("pause_at", ["read", "write"])
def test_reset_is_serialized_across_processes(pause_at: str) -> None:
    context = multiprocessing.get_context("fork")
    notifier = aiomulti.AioProcessNotifier()
    process = KeyboardProcess(
        notifier=notifier, device_path="/dev/test-keyboard",
        select_timeout=0.01, queue_timeout=0.01, write_retries=3, noop=False,
    )
    checkpoint = context.Event()
    resume = context.Event()
    released = context.Event()
    resetting = context.Event()
    reset_done = context.Event()
    reports = context.Queue()
    worker = context.Process(target=_run_child_keyboard, args=(process, checkpoint, resume, released, reports, pause_at))

    def clear_keyboard() -> None:
        resetting.set()
        process.send_clear_event()
        reset_done.set()

    resetter = threading.Thread(target=clear_keyboard, daemon=True)
    worker.start()
    try:
        process.send_key_event(ecodes.KEY_A, True)
        assert checkpoint.wait(5), "Worker did not reach the synchronized checkpoint"
        resetter.start()
        assert resetting.wait(5)
        if pause_at == "read":
            assert reset_done.wait(5), "LED reads must not hold the reset/write lock"
        else:
            assert not reset_done.wait(0.1), "Reset completed while an old-generation write was in progress"
        resume.set()
        assert reset_done.wait(5)
        assert released.wait(5), "Worker did not release the keyboard after reset"
        if pause_at == "write":
            assert reports.get(timeout=5) == (0, _report(4))
        assert reports.get(timeout=5) == (1, _report())
        if pause_at == "read":
            assert reports.get(timeout=5) == (1, _report())
        process.send_key_event(ecodes.KEY_B, True)
        process.send_key_event(ecodes.KEY_B, False)
        assert reports.get(timeout=5) == (1, _report(5))
        assert reports.get(timeout=5) == (1, _report())
    finally:
        resume.set()
        process._BaseDeviceProcess__stop_event.set()
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        if resetter.ident is not None:
            resetter.join(5)
        process._BaseDeviceProcess__events_queue.close()
        process._BaseDeviceProcess__events_queue.join_thread()
        reports.close()
        reports.join_thread()
        notifier._AioProcessNotifier__queue.close()
        notifier._AioProcessNotifier__queue.join_thread()
    assert worker.exitcode == 0
    assert not resetter.is_alive()


class _DrainLimitExceeded(BaseException):
    pass


@pytest.fixture
def low_level_keyboard(monkeypatch: pytest.MonkeyPatch) -> Iterable[KeyboardProcess]:
    notifier = aiomulti.AioProcessNotifier()
    monkeypatch.setattr(notifier, "notify", lambda mask=0: None)
    process = KeyboardProcess(
        notifier=notifier, device_path="/dev/test-keyboard",
        select_timeout=0.01, queue_timeout=0.01, write_retries=3, noop=False,
    )
    monkeypatch.setattr(process, "_BaseDeviceProcess__fd", 123)
    try:
        yield process
    finally:
        process._BaseDeviceProcess__events_queue.close()
        process._BaseDeviceProcess__events_queue.join_thread()
        notifier._AioProcessNotifier__queue.close()
        notifier._AioProcessNotifier__queue.join_thread()


@pytest.mark.parametrize("error", [errno.EAGAIN, errno.ESHUTDOWN])
def test_led_drain_stops_on_read_error(monkeypatch: pytest.MonkeyPatch, low_level_keyboard: KeyboardProcess, error: int) -> None:
    reader = Mock(side_effect=[OSError(error, "USB unavailable"), _DrainLimitExceeded("Read repeated after failure")])
    monkeypatch.setattr(device.select, "select", lambda *args: ([123], [], []))
    monkeypatch.setattr(device.os, "read", reader)
    low_level_keyboard._BaseDeviceProcess__read_reports()
    assert reader.call_count == 1


@pytest.mark.parametrize("interrupt", ["stop", "clear", "limit", "eof"])
def test_led_drain_is_bounded(monkeypatch: pytest.MonkeyPatch, low_level_keyboard: KeyboardProcess, interrupt: str) -> None:
    reads = 0

    def read_report(descriptor: int, size: int) -> bytes:
        nonlocal reads
        _ = (descriptor, size)
        reads += 1
        if reads > 64:
            raise _DrainLimitExceeded("LED drain exceeded its batch limit")
        if interrupt == "stop":
            low_level_keyboard._BaseDeviceProcess__stop_event.set()
        elif interrupt == "clear":
            low_level_keyboard.send_clear_event()
        return b"" if interrupt == "eof" else b"\x00"

    monkeypatch.setattr(device.select, "select", lambda *args: ([123], [], []))
    monkeypatch.setattr(device.os, "read", read_report)
    low_level_keyboard._BaseDeviceProcess__read_reports()
    assert reads == (64 if interrupt == "limit" else 1)


@pytest.mark.parametrize("interrupt", ["stop", "clear"])
def test_led_drain_cancellation_prevents_stale_write(
    monkeypatch: pytest.MonkeyPatch,
    low_level_keyboard: KeyboardProcess,
    interrupt: str,
) -> None:
    def read_report(descriptor: int, size: int) -> bytes:
        _ = (descriptor, size)
        if interrupt == "stop":
            low_level_keyboard._BaseDeviceProcess__stop_event.set()
        else:
            low_level_keyboard.send_clear_event()
        return b"\x00"

    writer = Mock()
    monkeypatch.setattr(low_level_keyboard, "_BaseDeviceProcess__ensure_device", lambda: True)
    monkeypatch.setattr(device.select, "select", lambda *args: ([123], [], []))
    monkeypatch.setattr(device.os, "read", read_report)
    monkeypatch.setattr(device.os, "write", writer)
    assert not low_level_keyboard._BaseDeviceProcess__write_ordered_report(_report(4), 0)
    writer.assert_not_called()


@pytest.mark.parametrize("read_result", [b"\x00", b"", errno.EAGAIN, errno.ESHUTDOWN])
def test_real_led_drain_preserves_key_transitions(
    monkeypatch: pytest.MonkeyPatch,
    low_level_keyboard: KeyboardProcess,
    read_result: bytes | int,
) -> None:
    reader = Mock()
    if isinstance(read_result, int):
        reader.side_effect = OSError(read_result, "USB unavailable")
    else:
        reader.return_value = read_result
    accepted = []

    def write_report(descriptor: int, report: bytes) -> int:
        _ = descriptor
        accepted.append(report)
        return len(report)

    monkeypatch.setattr(low_level_keyboard, "_BaseDeviceProcess__ensure_device", lambda: True)
    monkeypatch.setattr(device.select, "select", lambda *args: ([123], [], []))
    monkeypatch.setattr(device.os, "read", reader)
    monkeypatch.setattr(device.os, "write", write_report)
    assert low_level_keyboard._BaseDeviceProcess__write_ordered_report(_report(4), 0)
    assert low_level_keyboard._BaseDeviceProcess__write_ordered_report(_report(), 0)
    assert accepted == [_report(4), _report()]
    assert reader.call_count == (128 if read_result == b"\x00" else 2)


@pytest.mark.parametrize("blocked_at", ["logging", "status"])
def test_reset_does_not_wait_for_write_reporting(
    monkeypatch: pytest.MonkeyPatch,
    low_level_keyboard: KeyboardProcess,
    blocked_at: str,
) -> None:
    entered = threading.Event()
    resume = threading.Event()
    reset_done = threading.Event()
    results = []
    failures = []
    logger = Mock()

    def block_reporting(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)
        entered.set()
        if not resume.wait(5):
            raise RuntimeError("Reporting checkpoint timed out")

    monkeypatch.setattr(low_level_keyboard, "_BaseDeviceProcess__logger", logger)
    monkeypatch.setattr(low_level_keyboard, "_BaseDeviceProcess__ensure_device", lambda: True)
    monkeypatch.setattr(low_level_keyboard, "_BaseDeviceProcess__read_all_reports", lambda: None)
    monkeypatch.setattr(device.os, "write", lambda descriptor, report: 0 if blocked_at == "logging" else len(report))
    if blocked_at == "logging":
        logger.error.side_effect = block_reporting
    else:
        monkeypatch.setattr(low_level_keyboard._BaseDeviceProcess__state_flags, "update", block_reporting)

    def write_report() -> None:
        try:
            results.append(low_level_keyboard._BaseDeviceProcess__write_ordered_report(_report(4), 0))
        except Exception as error:
            failures.append(error)

    def clear_keyboard() -> None:
        low_level_keyboard.send_clear_event()
        reset_done.set()

    writer = threading.Thread(target=write_report, daemon=True)
    resetter = threading.Thread(target=clear_keyboard, daemon=True)
    writer.start()
    try:
        assert entered.wait(5)
        resetter.start()
        assert reset_done.wait(1), "Reset was blocked by logging/status after the USB syscall"
        assert writer.is_alive(), "Reporting must still be blocked at this checkpoint"
    finally:
        resume.set()
        writer.join(5)
        if resetter.ident is not None:
            resetter.join(5)
    assert not failures
    assert not writer.is_alive() and not resetter.is_alive()
    assert results == [blocked_at == "status"]
