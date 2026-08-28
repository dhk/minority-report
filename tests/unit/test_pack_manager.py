from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.pack_manager import (
    ManagerError,
    _table,
    inventory,
    quarantine_pack,
    run_pack,
)


def _manifest(bundle_id: str = "1.2.3-abcdef-20260729T000000Z") -> dict[str, object]:
    return {
        "format_version": 1,
        "bundle_id": bundle_id,
        "tool": {"name": "sample", "display_name": "Sample", "version": "1.2.3"},
        "source": {"commit": "abcdef123456", "dirty": False, "remote": None},
        "install": {
            "default_root": "~/src/sample",
            "repo_environment": "SAMPLE_REPO",
            "secrets_file": "~/.config/sample/secrets.env",
            "required_secrets": [],
        },
        "services": [],
    }


def _pack(directory: Path, *, unsafe_member: str | None = None) -> tuple[Path, str]:
    manifest = _manifest()
    bundle_id = str(manifest["bundle_id"])
    root = f"sample-{bundle_id}"
    archive = directory / f"{root}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, payload, mode in (
            (f"{root}/pack-manifest.json", json.dumps(manifest).encode(), 0o644),
            (f"{root}/install.py", b"#!/usr/bin/env python3\n", 0o755),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            bundle.addfile(info, io.BytesIO(payload))
        if unsafe_member:
            payload = b"unsafe\n"
            info = tarfile.TarInfo(unsafe_member)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".gz.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, root


def test_inventory_reads_manifest_and_verifies_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    archive, _root = _pack(tmp_path)

    records = inventory(tmp_path)

    assert len(records) == 1
    record = records[0]
    assert record.archive == archive
    assert record.tool == "sample"
    assert record.version == "1.2.3"
    assert record.checksum_state == "verified"
    assert record.extracted is None
    assert record.install_state == "not installed"
    assert "Sample" in _table(records)


def test_inventory_reports_missing_and_mismatched_checksums(tmp_path: Path) -> None:
    archive, _root = _pack(tmp_path)
    checksum = archive.with_suffix(".gz.sha256")
    checksum.unlink()

    assert inventory(tmp_path)[0].checksum_state == "missing"

    checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")
    assert inventory(tmp_path)[0].checksum_state == "mismatch"


def test_run_verifies_extracts_and_invokes_installer_from_transfer_directory(
    tmp_path: Path,
) -> None:
    _archive, root = _pack(tmp_path)
    record = inventory(tmp_path)[0]
    calls: list[tuple[list[str], Path]] = []

    def runner(command: list[str], *, cwd: Path, check: bool) -> SimpleNamespace:
        assert check is False
        calls.append((command, cwd))
        return SimpleNamespace(returncode=0)

    result = run_pack(record, tmp_path, ["--dry-run"], runner=runner)

    assert result == 0
    assert calls[0][1] == tmp_path
    assert calls[0][0][-2:] == [str(tmp_path / root / "install.py"), "--dry-run"]
    assert (tmp_path / root / "pack-manifest.json").is_file()


def test_run_refuses_modified_extracted_pack(tmp_path: Path) -> None:
    _archive, root = _pack(tmp_path)
    record = inventory(tmp_path)[0]
    run_pack(
        record,
        tmp_path,
        ["--dry-run"],
        runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    (tmp_path / root / "install.py").write_text("modified\n", encoding="utf-8")

    with pytest.raises(ManagerError, match="differs from archive"):
        run_pack(inventory(tmp_path)[0], tmp_path)


def test_run_refuses_path_traversal_archive(tmp_path: Path) -> None:
    _pack(tmp_path, unsafe_member="sample-1.2.3-abcdef-20260729T000000Z/../../escape")
    record = inventory(tmp_path)[0]

    with pytest.raises(ManagerError, match="unsafe archive path"):
        run_pack(record, tmp_path)

    assert not (tmp_path.parent / "escape").exists()


def test_quarantine_moves_only_transfer_artifacts_and_writes_restore_record(
    tmp_path: Path,
) -> None:
    archive, root = _pack(tmp_path)
    record = inventory(tmp_path)[0]
    run_pack(record, tmp_path, runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    trash = tmp_path / "trash"

    destination = quarantine_pack(inventory(tmp_path)[0], tmp_path, trash_root=trash)

    assert not archive.exists()
    assert not archive.with_suffix(".gz.sha256").exists()
    assert not (tmp_path / root).exists()
    restore = json.loads((destination / "restore.json").read_text(encoding="utf-8"))
    assert len(restore["moved"]) == 3
    assert {path.name for path in destination.iterdir()} >= {
        archive.name,
        archive.with_suffix(".gz.sha256").name,
        root,
        "restore.json",
    }


def test_invalid_tarball_is_listed_but_cannot_run(tmp_path: Path) -> None:
    archive = tmp_path / "not-a-pack.tar.gz"
    archive.write_bytes(b"not gzip")

    record = inventory(tmp_path)[0]

    assert record.error is not None
    assert record.tool == "invalid"
    with pytest.raises(ManagerError):
        run_pack(record, tmp_path, allow_unverified=True)
