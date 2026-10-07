import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

from contrib import analyze_keyboard_capture as analysis


def event(timestamp: float, code: int, state: bool, modifier: bool=False) -> dict:
    return {"time": timestamp, "type": "ModifierEvent" if modifier else "KeyEvent", "code": code, "state": state}


def write(timestamp: float, codes: list[int], modifiers: int=0, success: bool=True) -> dict:
    return {"time": timestamp, "hex": bytes([modifiers, 0, *codes, *([0] * (6 - len(codes)))]).hex(), "success": success}


def browser(records: list[tuple[float, str, bool]]) -> dict:
    return {"started": 100000, "stopped": 120000, "records": [
        {"kind": "keydown" if state else "keyup", "code": code, "epoch": timestamp * 1000}
        for timestamp, code, state in records
    ]}


KEYMAP = {"KeyZ": ("KeyEvent", 29), "KeyA": ("KeyEvent", 4), "ShiftLeft": ("ModifierEvent", 2)}
METADATA = {"started_at": 99, "ended_at": 121}


def test_arbitrary_overlap_and_modifier_reports() -> None:
    events = [event(101, 2, True, True), event(102, 29, True), event(102.01, 4, True),
              event(102.02, 29, False), event(102.03, 4, False), event(103, 2, False, True)]
    writes = [write(101.001, [], 2), write(102.001, [29], 2), write(102.011, [29, 4], 2),
              write(102.021, [0, 4], 2), write(102.031, [], 2), write(103.001, [])]
    result = analysis.compare_reports(events, writes)
    assert result["status"] == "matched"
    assert result["expected_reports"] == 6
    assert result["matched_event_to_write_ms"]["max"] == pytest.approx(1)


def test_missing_release_is_identified_without_prescribed_text() -> None:
    captured = browser([(101, "KeyZ", True), (101.1, "KeyA", True), (101.2, "KeyZ", False), (101.3, "KeyA", False)])
    events = [event(101.08, 29, True), event(101.18, 4, True), event(101.38, 4, False)]
    result = analysis.compare_transitions(captured, events, METADATA, KEYMAP)
    assert result["status"] == "differences"
    assert result["differences"][0]["kind"] == "delete"
    assert result["differences"][0]["browser"][0]["browser_code"] == "KeyZ"
    assert result["differences"][0]["browser"][0]["state"] is False


def test_capture_boundary_is_not_reported_as_dropped_key() -> None:
    captured = browser([(101, "KeyA", True), (101.1, "KeyA", False), (110, "KeyZ", True)])
    result = analysis.compare_transitions(
        captured, [event(101.08, 4, True), event(101.18, 4, False)], {"started_at": 99, "ended_at": 105}, KEYMAP,
    )
    assert result["status"] == "matched"
    assert result["outside_shared_window"][0]["browser_code"] == "KeyZ"


def test_repeat_does_not_require_additional_usb_presses() -> None:
    captured = browser([(101, "KeyA", True), (101.5, "KeyA", True), (103, "KeyA", False)])
    captured["records"][1]["repeat"] = True
    result = analysis.compare_transitions(captured, [event(101.08, 4, True), event(103.08, 4, False)], METADATA, KEYMAP)
    assert result["status"] == "matched"
    assert result["browser_events"] == 2


def test_failed_write_and_missing_release_report_are_separate() -> None:
    result = analysis.compare_reports([event(101, 4, True), event(102, 4, False)],
                                      [write(101.001, [4]), write(102.001, [], success=False)])
    assert result["failed_writes"] == 1
    assert result["status"] == "differences"
    assert result["differences"][0]["expected"][0]["hex"] == "0000000000000000"


def test_repress_and_six_slot_reset_follow_worker_policy() -> None:
    events = [event(100 + code, code, True) for code in range(4, 11)]
    events += [event(111, 10, True), {"time": 112, "type": "ClearEvent"}]
    reports = analysis.expected_reports(events)
    assert reports[6]["hex"] == "0000000000000000"
    assert reports[7]["hex"] == "00000a0000000000"
    assert reports[8]["hex"] == "0000000000000000"
    assert reports[9]["hex"] == "00000a0000000000"
    assert reports[10]["hex"] == "0000000000000000"


def test_keymap_uses_repository_csv() -> None:
    keymap = analysis.load_keymap(Path(__file__).resolve().parents[4] / "keymap.csv")
    assert keymap["ShiftLeft"] == ("ModifierEvent", 2)
    assert keymap["KeyZ"] == ("KeyEvent", 29)


def test_unknown_key_is_not_silently_counted_as_delivered() -> None:
    captured = browser([(101, "KeyA", True), (102, "UnknownKey", True)])
    result = analysis.compare_transitions(captured, [event(101.08, 4, True)], METADATA, KEYMAP)
    assert result["unsupported_keys"][0]["code"] == "UnknownKey"


def test_trace_parser_handles_fragmented_frames_without_executing_pickle() -> None:
    class_name = "KeyEvent"
    payload = pickle.dumps((0, SimpleNamespace(kind=class_name, code=29, state=True)), protocol=4)
    framed = len(payload).to_bytes(4, "big") + payload

    def read_line(timestamp: float, data: bytes) -> str:
        escaped = "".join(f"\\x{value:02x}" for value in data)
        return f'{timestamp:.6f} read(5, "{escaped}", {len(data)}) = {len(data)} <0.000010>'

    trace = "\n".join([read_line(101, framed[:7]), read_line(101.01, framed[7:]),
                       '101.020000 write(22, "\\x00\\x00\\x1d\\x00\\x00\\x00\\x00\\x00", 8) = 8 <0.000010>'])
    events, writes, issues = analysis.parse_trace(trace)
    assert issues == []
    assert events == [event(101.01, 29, True)]
    assert writes[0]["hex"] == "00001d0000000000"


def test_malformed_trace_is_reported_not_ignored() -> None:
    events, writes, issues = analysis.parse_trace("101.000000 read(5, <unfinished ...>\n")
    assert not events and not writes
    assert issues[0]["reason"] == "unparsed-syscall"


def test_led_reads_are_not_mistaken_for_queue_frames() -> None:
    trace = ('101.000000 read(22, "\\x02", 1) = 1 <0.000010>\n'
             '101.010000 write(22, "\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00", 8) = 8 <0.000010>')
    events, writes, issues = analysis.parse_trace(trace)
    assert not events and not issues
    assert writes[0]["success"]


def test_reordered_input_is_not_reported_as_matching() -> None:
    captured = browser([(101, "KeyZ", True), (101.01, "KeyA", True), (101.02, "KeyZ", False), (101.03, "KeyA", False)])
    events = [event(101.08, 4, True), event(101.09, 29, True), event(101.1, 29, False), event(101.11, 4, False)]
    assert analysis.compare_transitions(captured, events, METADATA, KEYMAP)["status"] == "differences"


def test_typing_statistics_accept_arbitrary_holds_and_order() -> None:
    captured = browser([(101, "KeyZ", True), (101.01, "KeyA", True), (101.02, "KeyZ", False), (104, "KeyA", False)])
    result = analysis.typing_statistics(captured, KEYMAP)
    assert result["max_overlapping_non_modifier_keys"] == 2
    assert result["non_modifier_presses"] == 2
    assert result["completed_hold_ms"]["max"] == pytest.approx(2990)
    assert result["keys_still_down_at_browser_stop"] == []
