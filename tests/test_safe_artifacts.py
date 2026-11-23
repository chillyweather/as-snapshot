"""Regression tests for the shared safe artifact publication policy.

Pinned here:

* A failure while creating the second staged file of a pair removes the
  first staged file again and never touches the destinations.
* The same cleanup protection applies to single-file publication.
* A successful pair publication leaves no staging leftovers.
* A pair lock whose acquisition fails never releases the lock again, so
  the original acquisition error is preserved unmasked.

The failures are injected offline by monkeypatching the module-level
``exclusive_temp`` name; no platform-specific behavior is exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoscout import safe_artifacts
from autoscout.safe_artifacts import pair_write_lock
from autoscout.safe_artifacts import publish_bundle
from autoscout.safe_artifacts import publish_pair
from autoscout.safe_artifacts import publish_single


def _leftovers(directory: Path) -> list[Path]:
    return [path for path in directory.iterdir() if path.name.endswith((".stage", ".backup"))]


def test_publish_pair_cleans_first_stage_when_second_staging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "pair-a.json"
    second = tmp_path / "pair-b.json"
    real_exclusive_temp = safe_artifacts.exclusive_temp
    calls = 0

    def failing_second_stage(directory: Path, base_name: str, suffix: str) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second staging failure")
        return real_exclusive_temp(directory, base_name, suffix)

    monkeypatch.setattr(safe_artifacts, "exclusive_temp", failing_second_stage)
    with pytest.raises(OSError, match="injected second staging failure"):
        publish_pair(first, second, b'{"first": true}\n', b'{"second": true}\n')
    assert calls == 2
    assert not first.exists()
    assert not second.exists()
    assert _leftovers(tmp_path) == []


def test_publish_single_cleans_staged_file_when_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "single.json"

    def failing_write(path: Path, content: str | bytes) -> None:
        raise OSError("injected write failure after staging")

    monkeypatch.setattr(safe_artifacts, "_write_content", failing_write)
    with pytest.raises(OSError, match="injected write failure"):
        publish_single(destination, b'{"single": true}\n')
    assert not destination.exists()
    assert _leftovers(tmp_path) == []


def test_exclusive_temp_cleans_up_when_close_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_close(file_descriptor: int) -> None:
        raise OSError("injected close failure")

    monkeypatch.setattr(safe_artifacts.os, "close", failing_close)
    with pytest.raises(OSError, match="injected close failure"):
        safe_artifacts.exclusive_temp(tmp_path, "base", ".stage")
    assert _leftovers(tmp_path) == []


def test_pair_write_lock_does_not_unlock_when_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "pair-a.json"
    second = tmp_path / "pair-b.json"
    unlock_calls = 0

    def failing_acquisition(file_descriptor: int) -> None:
        raise RuntimeError("original acquisition failure")

    def unlock_must_not_run(file_descriptor: int) -> None:
        nonlocal unlock_calls
        unlock_calls += 1
        raise AssertionError("_unlock_pair_descriptor ran after acquisition failure")

    monkeypatch.setattr(safe_artifacts, "_lock_pair_descriptor", failing_acquisition)
    monkeypatch.setattr(safe_artifacts, "_unlock_pair_descriptor", unlock_must_not_run)
    with pytest.raises(RuntimeError, match="original acquisition failure") as exc_info:
        with pair_write_lock(first, second):
            pass
    assert exc_info.value.args == ("original acquisition failure",)
    assert unlock_calls == 0


def test_publish_bundle_rolls_back_every_destination_after_replace_failure(tmp_path: Path) -> None:
    first = tmp_path / "bundle-a.json"
    second = tmp_path / "bundle-b.json"
    first.write_bytes(b"old-a\n")
    second.write_bytes(b"old-b\n")
    before = {path: path.read_bytes() for path in (first, second)}
    real_replace = safe_artifacts.staged_replace
    calls = 0

    def failing_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected bundle replacement failure")
        real_replace(source, destination)

    with pytest.raises(OSError, match="injected bundle replacement failure"):
        publish_bundle([first, second], [b"new-a\n", b"new-b\n"], replace=failing_replace)

    assert {path: path.read_bytes() for path in (first, second)} == before
    assert _leftovers(tmp_path) == []


def test_publish_bundle_persistent_forward_failure_restores_every_destination(tmp_path: Path) -> None:
    first = tmp_path / "bundle-a.json"
    second = tmp_path / "bundle-b.json"
    first.write_bytes(b"old-a\n")
    second.write_bytes(b"old-b\n")
    before = {path: path.read_bytes() for path in (first, second)}
    real_replace = safe_artifacts.staged_replace
    calls = 0

    def persistently_failing_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("persistent forward replacement failure")
        real_replace(source, destination)

    with pytest.raises(OSError, match="persistent forward replacement failure"):
        publish_bundle([first, second], [b"new-a\n", b"new-b\n"], replace=persistently_failing_replace)

    assert {path: path.read_bytes() for path in (first, second)} == before
    assert _leftovers(tmp_path) == []


def test_publish_bundle_retains_recovery_backup_when_trusted_recovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "bundle-a.json"
    second = tmp_path / "bundle-b.json"
    first.write_bytes(b"old-a\n")
    second.write_bytes(b"old-b\n")
    real_replace = safe_artifacts.staged_replace

    def failing_forward_replace(source: Path, destination: Path) -> None:
        if destination == second:
            raise OSError("forward replacement failed")
        real_replace(source, destination)

    def failing_trusted_recovery(source: Path, destination: Path) -> None:
        if destination == first:
            raise OSError("trusted recovery failed")
        real_replace(source, destination)

    monkeypatch.setattr(safe_artifacts, "_trusted_recovery_replace", failing_trusted_recovery)
    with pytest.raises(safe_artifacts.SafeArtifactRecoveryError, match="bundle recovery failed"):
        publish_bundle([first, second], [b"new-a\n", b"new-b\n"], replace=failing_forward_replace)

    assert first.read_bytes() == b"new-a\n"
    assert second.read_bytes() == b"old-b\n"
    assert list(tmp_path.glob(".*.backup"))
    assert not list(tmp_path.glob(".*.stage"))


def test_publish_bundle_retains_original_backup_after_wrong_recovery_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "bundle-a.json"
    second = tmp_path / "bundle-b.json"
    first.write_bytes(b"old-a\n")
    second.write_bytes(b"old-b\n")
    real_replace = safe_artifacts.staged_replace
    real_recovery = safe_artifacts._trusted_recovery_replace

    def failing_forward_replace(source: Path, destination: Path) -> None:
        if destination == second:
            raise OSError("forward replacement failed")
        real_replace(source, destination)

    def wrong_trusted_recovery(source: Path, destination: Path) -> None:
        if destination == first:
            destination.write_bytes(b"wrong-a\n")
            return
        real_recovery(source, destination)

    monkeypatch.setattr(safe_artifacts, "_trusted_recovery_replace", wrong_trusted_recovery)
    with pytest.raises(safe_artifacts.SafeArtifactRecoveryError, match="bundle recovery failed"):
        publish_bundle([first, second], [b"new-a\n", b"new-b\n"], replace=failing_forward_replace)

    assert first.read_bytes() == b"wrong-a\n"
    assert second.read_bytes() == b"old-b\n"
    backups = list(tmp_path.glob(".*.backup"))
    assert backups
    assert any(path.read_bytes() == b"old-a\n" for path in backups)
    assert not list(tmp_path.glob(".*.stage"))


def test_publish_pair_succeeds_and_leaves_no_leftovers(tmp_path: Path) -> None:
    first = tmp_path / "pair-a.json"
    second = tmp_path / "pair-b.json"
    publish_pair(first, second, b'{"first": true}\n', b'{"second": true}\n')
    assert first.read_bytes() == b'{"first": true}\n'
    assert second.read_bytes() == b'{"second": true}\n'
    assert _leftovers(tmp_path) == []
