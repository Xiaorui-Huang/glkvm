import argparse
import csv
import difflib
import json
import pickletools
import re
import statistics
from pathlib import Path
from typing import Any


SYSCALL = re.compile(r'^(\d+\.\d+) (read|write)\((\d+), "((?:\\x[0-9a-f]{2})*)", (\d+)\) = (-?\d+)(.*)$')
EVENT_TYPES = {"KeyEvent", "ModifierEvent", "ClearEvent", "ResetEvent"}


def decode_event(payload: bytes, timestamp: float) -> dict[str, Any]:
    opcodes = list(pickletools.genops(payload))
    names = [argument for opcode, argument, _ in opcodes if opcode.name in {"SHORT_BINUNICODE", "BINUNICODE"}]
    event_types = [name for name in names if name in EVENT_TYPES]
    if len(event_types) != 1:
        raise ValueError("Unsupported queue event")
    event: dict[str, Any] = {"time": timestamp, "type": event_types[0]}
    for index, (opcode, argument, _) in enumerate(opcodes):
        if opcode.name in {"SHORT_BINUNICODE", "BINUNICODE"} and argument in {"code", "state"}:
            following = next(entry for entry in opcodes[index + 1:] if entry[0].name != "MEMOIZE")
            if argument == "code":
                if following[0].name not in {"BININT", "BININT1", "BININT2"}:
                    raise ValueError("Unsupported key code encoding")
                event[argument] = following[1]
            else:
                if following[0].name not in {"NEWTRUE", "NEWFALSE"}:
                    raise ValueError("Unsupported key state encoding")
                event[argument] = following[0].name == "NEWTRUE"
    if event["type"] in {"KeyEvent", "ModifierEvent"} and not {"code", "state"} <= event.keys():
        raise ValueError("Incomplete key event")
    return event


def parse_trace(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    events = []
    writes = []
    issues = []
    buffers: dict[int, bytearray] = {}
    lines = text.splitlines()
    hid_descriptors = set()
    for line in lines:
        match = SYSCALL.fullmatch(line)
        if match is not None and match[2] == "write" and int(match[5]) == 8:
            hid_descriptors.add(int(match[3]))
    for number, line in enumerate(lines, 1):
        if line.startswith("strace:") or not line.strip():
            continue
        match = SYSCALL.fullmatch(line)
        if match is None:
            issues.append({"line": number, "reason": "unparsed-syscall"})
            continue
        timestamp_text, operation, descriptor_text, escaped, requested_text, returned_text, suffix = match.groups()
        timestamp = float(timestamp_text)
        descriptor = int(descriptor_text)
        returned = int(returned_text)
        data = bytes.fromhex(escaped.replace("\\x", ""))
        if operation == "write":
            writes.append({"time": timestamp, "fd": descriptor, "hex": data.hex(), "returned": returned,
                           "requested": int(requested_text), "success": returned == len(data) == 8})
            continue
        if descriptor in hid_descriptors:
            continue
        if returned <= 0:
            issues.append({"line": number, "reason": "read-error-or-eof", "time": timestamp})
            continue
        if returned != len(data):
            issues.append({"line": number, "reason": "truncated-read"})
            continue
        buffer = buffers.setdefault(descriptor, bytearray())
        buffer.extend(data)
        while len(buffer) >= 4:
            length = int.from_bytes(buffer[:4], "big")
            if not 1 <= length <= 1048576:
                issues.append({"line": number, "reason": "non-queue-read", "fd": descriptor})
                buffer.clear()
                break
            if len(buffer) < length + 4:
                break
            payload = bytes(buffer[4:length + 4])
            del buffer[:length + 4]
            try:
                events.append(decode_event(payload, timestamp))
            except (ValueError, StopIteration) as error:
                issues.append({"line": number, "reason": str(error)})
        _ = suffix
    for descriptor, buffer in buffers.items():
        if buffer:
            issues.append({"reason": "incomplete-queue-frame", "fd": descriptor, "bytes": len(buffer)})
    return (events, writes, issues)


def load_keymap(path: Path) -> dict[str, tuple[str, int]]:
    with path.open(newline="", encoding="utf-8") as source:
        return {
            row["web_name"]: ("ModifierEvent" if row["usb_key"].startswith("^") else "KeyEvent",
                              int(row["usb_key"].lstrip("^"), 16))
            for row in csv.DictReader(source)
        }


def signature(event: dict[str, Any]) -> tuple[str, int, bool]:
    return (event["type"], event.get("code", 0), event.get("state", False))


def browser_events(browser: dict[str, Any], keymap: dict[str, tuple[str, int]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = []
    unsupported = []
    for record in browser["records"]:
        if record["kind"] not in {"keydown", "keyup"}:
            continue
        if record["kind"] == "keydown" and record.get("repeat"):
            continue
        code = record.get("code")
        if code not in keymap:
            unsupported.append({"code": code, "epoch": record["epoch"]})
            continue
        event_type, usb_code = keymap[code]
        events.append({"time": record["epoch"] / 1000, "type": event_type, "code": usb_code,
                       "state": record["kind"] == "keydown", "browser_code": code,
                       "sequence": record.get("sequence")})
    return (events, unsupported)


def compare_transitions(browser: dict[str, Any], events: list[dict[str, Any]], metadata: dict[str, Any],
                        keymap: dict[str, tuple[str, int]]) -> dict[str, Any]:
    expected, unsupported = browser_events(browser, keymap)
    actual = [event for event in events if event["type"] in {"KeyEvent", "ModifierEvent"}]
    matching = difflib.SequenceMatcher(None, list(map(signature, expected)), list(map(signature, actual)), autojunk=False)
    offsets = [actual[block.b + index]["time"] - expected[block.a + index]["time"]
               for block in matching.get_matching_blocks() for index in range(block.size)]
    if not offsets:
        return {"status": "no-matching-anchor", "unsupported_keys": unsupported,
                "browser_events": len(expected), "worker_events": len(actual)}
    offset = statistics.median(offsets)
    lower = max(browser["started"] / 1000 + offset, metadata["started_at"])
    upper = min((browser.get("stopped") or browser["started"]) / 1000 + offset, metadata["ended_at"])
    outside = [event for event in expected if not lower <= event["time"] + offset <= upper]
    expected = [event for event in expected if lower <= event["time"] + offset <= upper]
    actual = [event for event in actual if lower <= event["time"] <= upper]
    matching = difflib.SequenceMatcher(None, list(map(signature, expected)), list(map(signature, actual)), autojunk=False)
    differences = [{"kind": kind, "browser": expected[start_expected:end_expected], "worker": actual[start_actual:end_actual]}
                   for kind, start_expected, end_expected, start_actual, end_actual in matching.get_opcodes() if kind != "equal"]
    return {"status": "differences" if differences else "matched", "browser_events": len(expected),
            "worker_events": len(actual), "estimated_clock_offset_plus_latency_ms": offset * 1000,
            "difference_groups": len(differences), "differences": differences[:20],
            "outside_shared_window": outside, "unsupported_keys": unsupported}


def expected_reports(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    modifiers = 0
    keys = [0] * 6
    reports = []

    def emit(timestamp: float) -> None:
        reports.append({"time": timestamp, "hex": bytes([modifiers, 0, *keys]).hex()})

    for event in events:
        timestamp = event["time"]
        if event["type"] in {"ClearEvent", "ResetEvent"}:
            modifiers = 0
            keys = [0] * 6
            emit(timestamp)
            continue
        code = event["code"]
        if event["type"] == "ModifierEvent":
            if modifiers & code:
                modifiers &= ~code
                emit(timestamp)
            if event["state"]:
                modifiers |= code
                emit(timestamp)
        else:
            if code in keys:
                keys[keys.index(code)] = 0
                emit(timestamp)
            elif event["state"] and 0 not in keys:
                keys = [0] * 6
                emit(timestamp)
            if event["state"]:
                keys[keys.index(0)] = code
                emit(timestamp)
    return reports


def compare_reports(events: list[dict[str, Any]], writes: list[dict[str, Any]]) -> dict[str, Any]:
    expected = expected_reports(events)
    accepted = [write for write in writes if write["success"]]
    matching = difflib.SequenceMatcher(
        None, [report["hex"] for report in expected], [write["hex"] for write in accepted], autojunk=False,
    )
    differences = [{"kind": kind, "expected": expected[start_expected:end_expected], "written": accepted[start_actual:end_actual]}
                   for kind, start_expected, end_expected, start_actual, end_actual in matching.get_opcodes() if kind != "equal"]
    delays = [(accepted[block.b + index]["time"] - expected[block.a + index]["time"]) * 1000
              for block in matching.get_matching_blocks() for index in range(block.size)]
    failed = [write for write in writes if not write["success"]]
    return {"status": "differences" if differences else "matched", "expected_reports": len(expected),
            "successful_writes": len(accepted), "failed_writes": len(failed), "failures": failed[:20],
            "difference_groups": len(differences), "differences": differences[:20],
            "matched_event_to_write_ms": {"min": min(delays), "max": max(delays)} if delays else None}


def typing_statistics(browser: dict[str, Any], keymap: dict[str, tuple[str, int]]) -> dict[str, Any]:
    events, _ = browser_events(browser, keymap)
    held: dict[str, float] = {}
    maximum = 0
    presses = 0
    holds = []
    for event in events:
        if event["type"] != "KeyEvent":
            continue
        code = event["browser_code"]
        if event["state"]:
            presses += 1
            held.setdefault(code, event["time"])
            maximum = max(maximum, len(held))
        elif code in held:
            holds.append((event["time"] - held.pop(code)) * 1000)
    return {"non_modifier_presses": presses, "max_overlapping_non_modifier_keys": maximum,
            "completed_hold_ms": {"min": min(holds), "max": max(holds)} if holds else None,
            "keys_still_down_at_browser_stop": list(held),
            "repeat_keydowns": sum(record["kind"] == "keydown" and bool(record.get("repeat")) for record in browser["records"])}


def analyze(browser: dict[str, Any], text: str, metadata: dict[str, Any], keymap: dict[str, tuple[str, int]]) -> dict[str, Any]:
    events, writes, issues = parse_trace(text)
    input_result = compare_transitions(browser, events, metadata, keymap)
    output_result = compare_reports(events, writes)
    warnings = ["Successful USB writes do not prove target receipt or application output.",
                "Clock alignment is estimated from matching events, not a network latency measurement.",
                "USB reconstruction assumes no keys were held when KVM tracing began.",
                "Browser auto-repeat keydowns are excluded from discrete transition comparison."]
    if not any(record["kind"] == "send-call" and record.get("transport") == "webrtc" for record in browser["records"]):
        warnings.append("WebRTC sends were not observed; transport capture is incomplete.")
    if issues:
        warnings.append("Trace parsing is incomplete; matches cannot establish complete delivery.")
    if input_result.get("outside_shared_window"):
        warnings.append("Some browser transitions are outside the shared recording window.")
    if input_result.get("unsupported_keys"):
        warnings.append("Some browser codes are not in the repository key map.")
    return {"browser_to_worker": input_result, "worker_to_usb": output_result,
            "typing": typing_statistics(browser, keymap),
            "trace_issues": issues, "warnings": warnings,
            "resets": [event for event in events if event["type"] in {"ClearEvent", "ResetEvent"}]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--keymap", type=Path, default=Path(__file__).resolve().parents[1] / "keymap.csv")
    args = parser.parse_args()
    browser = json.loads(args.browser.read_text(encoding="utf-8"))
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    if browser.get("active") or "ended_at" not in metadata:
        parser.error("Stop both captures before comparison")
    result = analyze(browser, args.trace.read_text(encoding="utf-8"), metadata, load_keymap(args.keymap))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
