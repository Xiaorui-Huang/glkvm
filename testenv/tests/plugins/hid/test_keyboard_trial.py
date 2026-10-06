import hashlib
import json
import py_compile
import tarfile
import zipfile
from pathlib import Path

import pytest

from contrib import keyboard_trial as trial_tool


@pytest.fixture
def prepared_trial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    package = tmp_path / "package"
    trial = tmp_path / "trial"
    package.mkdir()
    trial.mkdir()
    with zipfile.ZipFile(trial / "base.zip", "w") as base, zipfile.ZipFile(trial / "patch.zip", "w") as patch:
        for name in trial_tool.MODULES:
            source = tmp_path / f"{name}.py"
            source.write_text("value = 1\n")
            py_compile.compile(str(source), cfile=str(package / f"{name}.pyc"), doraise=True)
            base.writestr(f"{trial_tool.SOURCE_ROOT}/{name}.py", "value = 1\n")
            patch.writestr(f"{trial_tool.SOURCE_ROOT}/{name}.py", "value = 2\n")
    backup = tmp_path / "backup.tar"
    with tarfile.open(backup, "w") as archive:
        for name in trial_tool.MODULES:
            archive.add(package / f"{name}.pyc", arcname=f"{trial_tool.MODULE_ROOT}/{name}.pyc")
    monkeypatch.setattr(trial_tool, "BACKUP_SHA256", hashlib.sha256(backup.read_bytes()).hexdigest())
    trial_tool.prepare(trial / "base.zip", backup, package, trial)
    monkeypatch.setattr(trial_tool, "service", lambda action: None)
    monkeypatch.setattr(trial_tool, "wait_for_service", lambda running: {})
    return (trial, package)


def test_trial_roundtrip_restores_hashes_and_metadata(prepared_trial: tuple[Path, Path]) -> None:
    trial, package = prepared_trial
    manifest = trial_tool.read_manifest(trial, package)
    trial_tool.verify_files(trial, package, manifest, original=True)
    trial_tool.install(trial, package)
    trial_tool.verify_files(trial, package, manifest, original=False)
    assert json.loads((trial / "manifest.json").read_text())["status"] == "installed"
    trial_tool.rollback(trial, package)
    trial_tool.verify_files(trial, package, manifest, original=True)
    assert json.loads((trial / "manifest.json").read_text())["status"] == "restored"
    assert sorted(path.name for path in package.iterdir()) == ["device.pyc", "keyboard.pyc"]


@pytest.mark.parametrize("failure_at", ["stop", "replace", "start"])
def test_install_failure_automatically_restores_originals(
    prepared_trial: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, failure_at: str,
) -> None:
    trial, package = prepared_trial
    manifest = trial_tool.read_manifest(trial, package)
    actions = []
    original_replace = trial_tool.atomic_replace
    failed = False

    def replace(source: Path, target: Path, metadata: dict) -> None:
        if failure_at == "replace" and source.parent.name == "compiled" and source.name == "keyboard.pyc":
            raise RuntimeError("Injected replacement failure")
        original_replace(source, target, metadata)

    def service(action: str) -> None:
        nonlocal failed
        actions.append(action)
        if failure_at == "stop" and action == "stop":
            raise RuntimeError("Injected stop failure")
        if failure_at == "start" and action == "start" and not failed:
            failed = True
            raise RuntimeError("Injected startup failure")

    monkeypatch.setattr(trial_tool, "atomic_replace", replace)
    monkeypatch.setattr(trial_tool, "service", service)
    with pytest.raises(RuntimeError, match="Injected"):
        trial_tool.install(trial, package)
    trial_tool.verify_files(trial, package, manifest, original=True)
    assert actions[-2:] == (["stop"] if failure_at == "stop" else ["stop", "start"])
    assert json.loads((trial / "manifest.json").read_text())["status"] == ("stop-failed" if failure_at == "stop" else "restored")


@pytest.mark.parametrize("damage", ["original", "compiled", "production"])
def test_install_refuses_damaged_files_before_stop(
    prepared_trial: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    trial, package = prepared_trial
    location = package if damage == "production" else trial / damage
    (location / "device.pyc").write_bytes(b"damaged")
    actions = []
    monkeypatch.setattr(trial_tool, "service", actions.append)
    with pytest.raises(RuntimeError):
        trial_tool.install(trial, package)
    assert not actions
