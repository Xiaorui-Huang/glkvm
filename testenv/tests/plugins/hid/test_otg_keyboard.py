import asyncio
import logging
import queue
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
) -> tuple[list[bytes], list[bytes], dict[str, Any]]:
    process = KeyboardProcess(
        notifier=aiomulti.AioProcessNotifier(),
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

    event_queue = process._BaseDeviceProcess__events_queue
    monkeypatch.setattr(process, "_BaseDeviceProcess__events_queue", Mock(
        get=get_event, get_nowait=get_nowait, put_nowait=pending.append,
    ))
    monkeypatch.setattr(process, "_BaseDeviceProcess__fd", 123)
    monkeypatch.setattr(process, "_BaseDeviceProcess__ensure_device", ensure_device)
    monkeypatch.setattr(process, "_BaseDeviceProcess__read_all_reports", lambda: None)
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
