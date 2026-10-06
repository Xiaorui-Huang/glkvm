import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path("/userdata/glkvm-trials/keyboard-638c3a0c")


def duration_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Duration must be an integer") from error
    if not 1 <= seconds <= 600:
        raise argparse.ArgumentTypeError("Duration must be from 1 to 600 seconds")
    return seconds


def find_worker() -> tuple[int, list[int]]:
    workers = []
    for process in Path("/proc").iterdir():
        if process.name.isdigit():
            try:
                command = (process / "cmdline").read_bytes().split(b"\x00", 1)[0]
            except OSError:
                continue
            if command.startswith(b"kvmd/hid-keyboard:"):
                workers.append(process)
    if len(workers) != 1:
        raise RuntimeError("Expected exactly one keyboard worker")
    worker = workers[0]
    descriptors = []
    hid_found = False
    for entry in (worker / "fd").iterdir():
        try:
            target = os.readlink(entry)
            flags = next(line.split()[1] for line in (worker / "fdinfo" / entry.name).read_text().splitlines() if line.startswith("flags:"))
        except (OSError, StopIteration):
            continue
        if target.startswith("/dev/hidg"):
            descriptors.append(int(entry.name))
            hid_found = True
        elif target.startswith("pipe:") and int(flags, 8) & os.O_ACCMODE == os.O_RDONLY:
            descriptors.append(int(entry.name))
    if not hid_found:
        raise RuntimeError("Keyboard worker has no open HID endpoint")
    return (int(worker.name), sorted(descriptors))


def record(directory: Path) -> None:
    metadata = json.loads((directory / "metadata.json").read_text())
    metadata["supervisor_pid"] = os.getpid()
    metadata["started_at"] = time.time()
    with (directory / "trace.txt").open("xb") as output:
        process = subprocess.Popen([
            "strace", "-p", str(metadata["worker_pid"]), "-ttt", "-T", "-xx", "-s", "4096",
            "-e", "trace=read,write,writev",
            "-e", "trace-fds=" + ",".join(map(str, metadata["descriptors"])),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=output)
        metadata["tracer_pid"] = process.pid
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

        def interrupt(signum: int, frame: object) -> None:
            _ = (signum, frame)
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        try:
            process.communicate(timeout=metadata.get("duration_seconds", 90))
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        finally:
            metadata["ended_at"] = time.time()
            metadata["tracer_returncode"] = process.returncode
            (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "run", "status", "stop"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--seconds", type=duration_seconds, default=360)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "start":
        worker, descriptors = find_worker()
        directory = ROOT / time.strftime("capture-%Y%m%dT%H%M%SZ", time.gmtime())
        directory.mkdir(mode=0o700)
        metadata = {"worker_pid": worker, "descriptors": descriptors, "duration_seconds": args.seconds}
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        with (directory / "supervisor.log").open("xb") as output:
            supervisor = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "run", "--directory", str(directory)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
            )
        print(json.dumps({"directory": str(directory), "supervisor_pid": supervisor.pid, **metadata}))
    elif args.action == "run":
        if args.directory is None:
            parser.error("--directory required")
        record(args.directory)
    else:
        if args.directory is None or args.directory.parent != ROOT or not args.directory.name.startswith("capture-"):
            parser.error("A capture directory under the private trial directory is required")
        metadata = json.loads((args.directory / "metadata.json").read_text())
        if args.action == "stop" and "ended_at" not in metadata:
            pid = metadata["supervisor_pid"]
            command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\x00")
            if str(Path(__file__).resolve()).encode() not in command or str(args.directory).encode() not in command:
                raise RuntimeError("Capture supervisor PID no longer identifies this capture")
            os.kill(pid, signal.SIGTERM)
        print(json.dumps(metadata))


if __name__ == "__main__":
    main()
