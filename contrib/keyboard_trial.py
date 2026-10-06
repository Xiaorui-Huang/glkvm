import argparse
import fcntl
import hashlib
import http.client
import importlib
import importlib.abc
import importlib.util
import json
import marshal
import os
import py_compile
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import zipfile
from pathlib import Path


MODULE_ROOT = "usr/lib/python3.12/site-packages/kvmd/plugins/hid/otg"
SOURCE_ROOT = "kvmd/plugins/hid/otg"
MODULES = ("device", "keyboard")
BACKUP_SHA256 = "20c2a81e89cde67c83c46b4f577d4ddc39742e1774990568f9e8eff40c254923"


def code_fingerprint(code: types.CodeType) -> tuple:
    constants = tuple(code_fingerprint(value) if isinstance(value, types.CodeType) else value for value in code.co_consts)
    return (
        code.co_code, constants, code.co_names, code.co_varnames,
        code.co_freevars, code.co_cellvars, code.co_argcount,
        code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags,
        code.co_exceptiontable,
    )


def read_bytecode(payload: bytes) -> types.CodeType:
    if payload[:4] != importlib.util.MAGIC_NUMBER:
        raise RuntimeError("Bytecode Python version mismatch")
    code = marshal.loads(payload[16:])
    if not isinstance(code, types.CodeType):
        raise RuntimeError("Invalid module bytecode")
    return code


def compare(base_archive: Path, backup_archive: Path, package: Path) -> None:
    if hashlib.sha256(backup_archive.read_bytes()).hexdigest() != BACKUP_SHA256:
        raise RuntimeError("Baseline archive checksum mismatch")
    with zipfile.ZipFile(base_archive) as sources, tarfile.open(backup_archive) as backup:
        for name in MODULES:
            target = package / f"{name}.pyc"
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(f"Unexpected installed file: {target}")
            if (package / f"{name}.py").exists():
                raise RuntimeError(f"Unexpected source override for {name}")
            if any((package / "__pycache__").glob(f"{name}.*.pyc")):
                raise RuntimeError(f"Unexpected cached bytecode for {name}")
            members = [member for member in backup.getmembers() if member.name.lstrip("/") == f"{MODULE_ROOT}/{name}.pyc"]
            if len(members) != 1 or not members[0].isfile():
                raise RuntimeError(f"Missing unique baseline for {name}")
            original = backup.extractfile(members[0])
            if original is None:
                raise RuntimeError(f"Can't read baseline for {name}")
            installed = target.read_bytes()
            if installed != original.read():
                raise RuntimeError(f"Installed {name} changed since the baseline backup")
            source = sources.read(f"{SOURCE_ROOT}/{name}.py")
            fingerprint = code_fingerprint(read_bytecode(installed))
            matching = [
                level for level in range(3)
                if code_fingerprint(compile(source, str(target), "exec", optimize=level)) == fingerprint
            ]
            if not matching:
                raise RuntimeError(f"Complete deployed {name} bytecode differs from the pre-patch source; deployment blocked")
            print(f"{name}: backup exact match; complete source/bytecode match at optimization {matching}", flush=True)


def file_metadata(path: Path) -> dict:
    info = path.stat()
    return {"mode": info.st_mode & 0o7777, "uid": info.st_uid, "gid": info.st_gid, "mtime_ns": info.st_mtime_ns}


def restore_metadata(path: Path, metadata: dict) -> None:
    os.chmod(path, metadata["mode"])
    os.chown(path, metadata["uid"], metadata["gid"])
    os.utime(path, ns=(path.stat().st_atime_ns, metadata["mtime_ns"]))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(trial: Path, manifest: dict) -> None:
    temporary = trial / "manifest.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, trial / "manifest.json")


def prepare(base_archive: Path, backup_archive: Path, package: Path, trial: Path) -> None:
    if (trial / "manifest.json").exists():
        raise RuntimeError("Trial already prepared; don't overwrite its originals")
    compare(base_archive, backup_archive, package)
    manifest = {
        "package": str(package), "status": "prepared",
        "directory": file_metadata(package), "files": {},
        "patch_archive_sha256": digest(trial / "patch.zip"),
    }
    for directory in ("original", "compiled", "source"):
        (trial / directory).mkdir(mode=0o700, exist_ok=True)
    with zipfile.ZipFile(trial / "patch.zip") as patches:
        for name in MODULES:
            target = package / f"{name}.pyc"
            original = trial / "original" / target.name
            shutil.copy2(target, original)
            restore_metadata(original, file_metadata(target))
            source = trial / "source" / f"{name}.py"
            source.write_bytes(patches.read(f"{SOURCE_ROOT}/{name}.py"))
            compiled = trial / "compiled" / target.name
            filename = read_bytecode(original.read_bytes()).co_filename
            py_compile.compile(str(source), cfile=str(compiled), dfile=filename, doraise=True, optimize=0)
            os.chmod(source, 0o600)
            os.chmod(compiled, 0o600)
            if digest(original) != digest(target):
                raise RuntimeError("Original changed during preparation")
            manifest["files"][target.name] = {
                **file_metadata(target), "original_sha256": digest(original),
                "patch_sha256": digest(compiled),
            }
    write_manifest(trial, manifest)
    print("Prepared exact originals, device-compiled replacements, and manifest; production unchanged", flush=True)


def read_manifest(trial: Path, package: Path) -> dict:
    manifest = json.loads((trial / "manifest.json").read_text())
    if manifest["package"] != str(package) or set(manifest["files"]) != {f"{name}.pyc" for name in MODULES}:
        raise RuntimeError("Unexpected rollback manifest paths")
    for filename, metadata in manifest["files"].items():
        if digest(trial / "original" / filename) != metadata["original_sha256"]:
            raise RuntimeError(f"Original backup damaged: {filename}")
        if (package / filename).is_symlink() or (package / filename.replace(".pyc", ".py")).exists():
            raise RuntimeError(f"Unexpected production override: {filename}")
        if any((package / "__pycache__").glob(f"{filename[:-4]}.*.pyc")):
            raise RuntimeError(f"Unexpected production cache: {filename}")
    return manifest


def atomic_replace(source: Path, target: Path, metadata: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".keyboard-trial-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copyfile(source, temporary)
        restore_metadata(temporary, metadata)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def verify_files(trial: Path, package: Path, manifest: dict, original: bool) -> None:
    key = "original_sha256" if original else "patch_sha256"
    for filename, metadata in manifest["files"].items():
        target = package / filename
        if digest(target) != metadata[key]:
            raise RuntimeError(f"Installed checksum mismatch: {filename}")
        if file_metadata(target) != {name: metadata[name] for name in ("mode", "uid", "gid", "mtime_ns")}:
            raise RuntimeError(f"Installed metadata mismatch: {filename}")
    if file_metadata(package) != manifest["directory"]:
        raise RuntimeError("Installed directory metadata mismatch")
    print("Verified installed module hashes and metadata: " + ("original" if original else "trial"), flush=True)


def worker_pids() -> dict[str, list[int]]:
    workers = {name: [] for name in ("main", "hid-keyboard", "hid-mouse", "hid-touch")}
    for directory in Path("/proc").iterdir():
        if directory.name.isdigit():
            try:
                command = (directory / "cmdline").read_bytes().split(b"\x00", 1)[0].decode()
            except (OSError, UnicodeDecodeError):
                continue
            for name in workers:
                if command.startswith(f"kvmd/{name}:"):
                    workers[name].append(int(directory.name))
    return workers


def daemon_request() -> int:
    connection = http.client.HTTPConnection("localhost", timeout=2)
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(2)
    try:
        connection.sock.connect("/run/kvmd/kvmd.sock")
        connection.request("GET", "/hid")
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def wait_for_service(running: bool) -> dict:
    deadline = time.monotonic() + 25
    pause = threading.Event()
    while time.monotonic() < deadline:
        workers = worker_pids()
        if not running and not any(workers.values()):
            return workers
        if running and len(workers["main"]) == 1 and workers["hid-keyboard"] and len(workers["hid-mouse"]) >= 2 and workers["hid-touch"]:
            try:
                status = daemon_request()
                if status in (200, 401, 403):
                    print(f"KVMD responsive (HTTP {status}); workers {workers}", flush=True)
                    return workers
            except (OSError, http.client.HTTPException):
                pass
        pause.wait(0.1)
    raise RuntimeError(f"KVMD did not become {'ready' if running else 'stopped'} within 25 seconds")


def service(action: str) -> None:
    subprocess.run(["/etc/init.d/S98kvmd", action], check=True, timeout=20)
    wait_for_service(action == "start")


def smoke(trial: Path) -> None:
    class StagedFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path: object, target: object=None) -> object:
            _ = (path, target)
            if fullname in {f"kvmd.plugins.hid.otg.{name}" for name in MODULES}:
                return importlib.util.spec_from_file_location(fullname, trial / "compiled" / f"{fullname.rsplit('.', 1)[1]}.pyc")
            return None

    finder = StagedFinder()
    sys.meta_path.insert(0, finder)
    try:
        keyboard = importlib.import_module("kvmd.plugins.hid.otg.keyboard")
    finally:
        sys.meta_path.remove(finder)
    for name in MODULES:
        module = sys.modules[f"kvmd.plugins.hid.otg.{name}"]
        if Path(module.__file__).resolve() != (trial / "compiled" / f"{name}.pyc").resolve():
            raise RuntimeError(f"Smoke check didn't load the staged {name}")
    common = sys.modules["kvmd.plugins.hid.otg.device"]
    from kvmd import aiomulti
    from kvmd.plugins.hid.otg.events import make_keyboard_event
    from evdev import ecodes
    notifier = aiomulti.AioProcessNotifier()
    process = keyboard.KeyboardProcess(
        notifier=notifier, device_path="/dev/NOT-OPENED", select_timeout=0.1,
        queue_timeout=0.1, write_retries=3, noop=True,
    )
    try:
        if not isinstance(process, common.BaseDeviceProcess) or not process._BaseDeviceProcess__ensure_report_order:
            raise RuntimeError("Staged keyboard didn't enable ordered delivery")
        press = list(process._process_event(make_keyboard_event(ecodes.KEY_A, True)))
        release = list(process._process_event(make_keyboard_event(ecodes.KEY_A, False)))
        if press != [bytes([0, 0, 4, 0, 0, 0, 0, 0])] or release != [bytes(8)]:
            raise RuntimeError("Staged keyboard report mismatch")
    finally:
        process._BaseDeviceProcess__events_queue.close()
        process._BaseDeviceProcess__events_queue.join_thread()
        notifier._AioProcessNotifier__queue.close()
        notifier._AioProcessNotifier__queue.join_thread()
    print("Staged modules import against device dependencies; pure report checks pass; no USB opened", flush=True)


def rollback(trial: Path, package: Path) -> None:
    manifest = read_manifest(trial, package)
    service("stop")
    for filename, metadata in manifest["files"].items():
        atomic_replace(trial / "original" / filename, package / filename, metadata)
    restore_metadata(package, manifest["directory"])
    verify_files(trial, package, manifest, original=True)
    manifest["status"] = "restored"
    write_manifest(trial, manifest)
    service("start")
    print("ROLLBACK COMPLETE: original modules restored and KVMD responsive", flush=True)


def install(trial: Path, package: Path) -> None:
    manifest = read_manifest(trial, package)
    if manifest["status"] not in ("prepared", "restored"):
        raise RuntimeError("Trial isn't in a deployable state")
    for filename, metadata in manifest["files"].items():
        if digest(trial / "compiled" / filename) != metadata["patch_sha256"]:
            raise RuntimeError(f"Staged patch damaged: {filename}")
    verify_files(trial, package, manifest, original=True)
    wait_for_service(True)
    manifest["status"] = "stopping"
    write_manifest(trial, manifest)
    try:
        service("stop")
    except BaseException:
        manifest["status"] = "stop-failed"
        write_manifest(trial, manifest)
        verify_files(trial, package, manifest, original=True)
        print("Stop failed; no production files replaced; service state needs checking", flush=True)
        raise
    try:
        manifest["status"] = "installing"
        write_manifest(trial, manifest)
        for filename, metadata in manifest["files"].items():
            atomic_replace(trial / "compiled" / filename, package / filename, metadata)
        restore_metadata(package, manifest["directory"])
        verify_files(trial, package, manifest, original=False)
        service("start")
        manifest["status"] = "installed"
        write_manifest(trial, manifest)
    except BaseException:
        print("Install failed; restoring originals before returning", flush=True)
        rollback(trial, package)
        raise
    print("TRIAL INSTALLED: only device.pyc and keyboard.pyc replaced; no firmware or configuration changes", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_archive", type=Path)
    parser.add_argument("backup_archive", type=Path)
    parser.add_argument("--package", type=Path, default=Path("/") / MODULE_ROOT)
    parser.add_argument("--action", choices=("compare", "prepare", "smoke", "install", "rollback", "verify"), default="compare")
    parser.add_argument("--trial", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with (args.trial / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "compare":
            compare(args.base_archive, args.backup_archive, args.package)
        elif args.action == "prepare":
            prepare(args.base_archive, args.backup_archive, args.package, args.trial)
        elif args.action == "smoke":
            smoke(args.trial)
        elif args.action == "install":
            install(args.trial, args.package)
        elif args.action == "rollback":
            rollback(args.trial, args.package)
        else:
            manifest = read_manifest(args.trial, args.package)
            verify_files(args.trial, args.package, manifest, original=manifest["status"] != "installed")
            wait_for_service(True)


if __name__ == "__main__":
    main()
