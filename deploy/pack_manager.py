#!/usr/bin/env python3
# tool-pack-manager-format: 1
"""Inspect, run, and recoverably remove self-installing tool packs.

The manager is standard-library-only so it can be copied to an Ubuntu host
before any project dependencies are installed. It operates only on transfer
artifacts in one explicitly selected directory; installed releases and
application data are outside its authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT_VERSION = 1
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ManagerError(RuntimeError):
    """A pack cannot be inspected or acted upon safely."""


@dataclass(frozen=True)
class PackRecord:
    key: str
    tool: str
    display_name: str
    bundle_id: str
    version: str
    commit: str
    dirty: bool
    archive: Path | None
    checksum: Path | None
    extracted: Path | None
    checksum_state: str
    install_state: str
    size_bytes: int
    error: str | None = None

    def public(self) -> dict[str, object]:
        value = asdict(self)
        for key in ("archive", "checksum", "extracted"):
            path = value[key]
            value[key] = str(path) if path else None
        return value


def _load_manifest_bytes(payload: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagerError(f"{source} has an invalid pack manifest: {exc}") from exc
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        raise ManagerError(f"{source} has an unsupported pack manifest")
    tool = value.get("tool")
    source_info = value.get("source")
    install = value.get("install")
    if (
        not isinstance(tool, dict)
        or not isinstance(source_info, dict)
        or not isinstance(install, dict)
    ):
        raise ManagerError(f"{source} manifest is missing tool, source, or install metadata")
    for mapping, keys in (
        (value, ("bundle_id",)),
        (tool, ("name", "display_name", "version")),
        (source_info, ("commit",)),
        (install, ("default_root",)),
    ):
        for key in keys:
            if not isinstance(mapping.get(key), str) or not str(mapping[key]).strip():
                raise ManagerError(f"{source} manifest has invalid {key!r}")
    return value


def _manifest_from_directory(directory: Path) -> dict[str, Any]:
    manifest = directory / "pack-manifest.json"
    if not manifest.is_file():
        raise ManagerError(f"{directory.name} has no pack-manifest.json")
    return _load_manifest_bytes(manifest.read_bytes(), str(directory))


def _archive_manifest(archive: Path) -> tuple[str, dict[str, Any]]:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            candidates = [
                member
                for member in bundle.getmembers()
                if len(PurePosixPath(member.name).parts) == 2
                and PurePosixPath(member.name).name == "pack-manifest.json"
                and member.isfile()
            ]
            if len(candidates) != 1:
                raise ManagerError(f"{archive.name} must contain one top-level pack-manifest.json")
            member = candidates[0]
            stream = bundle.extractfile(member)
            if stream is None:
                raise ManagerError(f"cannot read {member.name} from {archive.name}")
            root = PurePosixPath(member.name).parts[0]
            return root, _load_manifest_bytes(stream.read(1024 * 1024), archive.name)
    except (OSError, tarfile.TarError) as exc:
        raise ManagerError(f"cannot inspect {archive.name}: {exc}") from exc


def _checksum_state(archive: Path, checksum: Path) -> str:
    if not checksum.is_file():
        return "missing"
    try:
        parts = checksum.read_text(encoding="utf-8").strip().split()
    except OSError:
        return "unreadable"
    if len(parts) != 2 or not _DIGEST.fullmatch(parts[0]) or parts[1] != archive.name:
        return "invalid"
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return "unreadable"
    return "verified" if digest.hexdigest() == parts[0] else "mismatch"


def _install_state(manifest: Mapping[str, Any]) -> str:
    install = manifest.get("install")
    if not isinstance(install, dict):
        return "unknown"
    install_root = Path(os.path.expandvars(str(install.get("default_root", "")))).expanduser()
    bundle_id = str(manifest.get("bundle_id", ""))
    release = install_root / "releases" / bundle_id
    current = install_root / "current"
    if current.is_symlink():
        try:
            if current.resolve() == release.resolve() and release.is_dir():
                return "current"
        except OSError:
            pass
    return "installed" if release.is_dir() else "not installed"


def _record(
    root_name: str,
    manifest: Mapping[str, Any],
    *,
    archive: Path | None,
    checksum: Path | None,
    extracted: Path | None,
) -> PackRecord:
    tool = manifest["tool"]
    source = manifest["source"]
    assert isinstance(tool, dict) and isinstance(source, dict)
    bundle_id = str(manifest["bundle_id"])
    expected_root = f"{tool['name']}-{bundle_id}"
    if root_name != expected_root:
        raise ManagerError(f"pack root is {root_name!r}, expected {expected_root!r}")
    return PackRecord(
        key=root_name,
        tool=str(tool["name"]),
        display_name=str(tool["display_name"]),
        bundle_id=bundle_id,
        version=str(tool["version"]),
        commit=str(source["commit"]),
        dirty=bool(source.get("dirty", False)),
        archive=archive,
        checksum=checksum,
        extracted=extracted,
        checksum_state=(
            _checksum_state(archive, checksum)
            if archive is not None and checksum is not None
            else "not applicable"
        ),
        install_state=_install_state(manifest),
        size_bytes=archive.stat().st_size if archive is not None else 0,
    )


def inventory(directory: Path) -> list[PackRecord]:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ManagerError(f"pack directory does not exist: {directory}")
    records: dict[str, PackRecord] = {}
    for archive in sorted(directory.glob("*.tar.gz")):
        checksum = archive.with_suffix(archive.suffix + ".sha256")
        try:
            root_name, manifest = _archive_manifest(archive)
            candidate = directory / root_name
            extracted: Path | None = candidate if candidate.is_dir() else None
            records[root_name] = _record(
                root_name,
                manifest,
                archive=archive,
                checksum=checksum,
                extracted=extracted,
            )
        except ManagerError as exc:
            records[archive.name] = PackRecord(
                key=archive.name,
                tool="invalid",
                display_name=archive.name,
                bundle_id="—",
                version="—",
                commit="—",
                dirty=False,
                archive=archive,
                checksum=checksum if checksum.exists() else None,
                extracted=None,
                checksum_state="invalid",
                install_state="unknown",
                size_bytes=archive.stat().st_size,
                error=str(exc),
            )
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or not (child / "pack-manifest.json").is_file():
            continue
        try:
            manifest = _manifest_from_directory(child)
        except ManagerError:
            continue
        existing = records.get(child.name)
        if existing is not None:
            records[child.name] = PackRecord(**{**asdict(existing), "extracted": child})
        else:
            records[child.name] = _record(
                child.name,
                manifest,
                archive=None,
                checksum=None,
                extracted=child,
            )
    return sorted(records.values(), key=lambda item: (item.tool, item.bundle_id, item.key))


def _table(records: Sequence[PackRecord]) -> str:
    headings = ("#", "TOOL", "VERSION / BUNDLE", "CHECKSUM", "EXTRACTED", "INSTALL")
    rows = [
        (
            str(index),
            record.display_name,
            f"{record.version} / {record.bundle_id}",
            record.checksum_state,
            "yes" if record.extracted else "no",
            record.install_state,
        )
        for index, record in enumerate(records, 1)
    ]
    widths = (
        [
            max(len(headings[column]), *(len(row[column]) for row in rows))
            for column in range(len(headings))
        ]
        if rows
        else [len(value) for value in headings]
    )
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headings))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows
    )
    return "\n".join(lines)


def _select(records: Sequence[PackRecord], selector: str) -> PackRecord:
    if selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(records):
            return records[index - 1]
    matches = [
        record
        for record in records
        if selector in {record.key, record.bundle_id}
        or (record.archive is not None and selector == record.archive.name)
    ]
    if not matches:
        raise ManagerError(f"no pack matches {selector!r}")
    if len(matches) > 1:
        raise ManagerError(f"selector {selector!r} matches more than one pack")
    return matches[0]


def _validate_archive(archive: Path, expected_root: str) -> None:
    total = 0
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ManagerError(f"unsafe archive path: {member.name!r}")
                if path.parts[0] != expected_root:
                    raise ManagerError(f"archive member escapes pack root: {member.name!r}")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ManagerError(
                        f"archive contains unsupported special entry: {member.name!r}"
                    )
                if not member.isdir() and not member.isfile():
                    raise ManagerError(f"archive contains unsupported entry: {member.name!r}")
                total += member.size
                if total > MAX_UNPACKED_BYTES:
                    raise ManagerError("archive exceeds the 512 MB unpacked safety limit")
    except (OSError, tarfile.TarError) as exc:
        raise ManagerError(f"cannot validate {archive.name}: {exc}") from exc


def _extract(record: PackRecord, directory: Path) -> Path:
    if record.extracted is not None:
        return record.extracted
    if record.archive is None:
        raise ManagerError("pack has neither an archive nor an extracted directory")
    _validate_archive(record.archive, record.key)
    destination = directory / record.key
    if destination.exists():
        raise ManagerError(f"extraction target already exists: {destination}")
    staging = Path(tempfile.mkdtemp(prefix=".tool-pack-extract-", dir=directory))
    try:
        with tarfile.open(record.archive, "r:gz") as bundle:
            bundle.extractall(staging, filter="data")
        unpacked = staging / record.key
        _manifest_from_directory(unpacked)
        unpacked.rename(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


def _verify_extracted(archive: Path, extracted: Path, expected_root: str) -> None:
    _validate_archive(archive, expected_root)
    expected: set[Path] = set()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                relative_parts = PurePosixPath(member.name).parts[1:]
                if not relative_parts:
                    continue
                relative = Path(*relative_parts)
                expected.add(relative)
                local = extracted / relative
                if member.isdir():
                    if not local.is_dir():
                        raise ManagerError(f"extracted directory differs from archive: {relative}")
                    continue
                if not local.is_file() or local.is_symlink() or local.stat().st_size != member.size:
                    raise ManagerError(f"extracted file differs from archive: {relative}")
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ManagerError(f"cannot verify archive member: {member.name}")
                archive_digest = hashlib.sha256()
                local_digest = hashlib.sha256()
                while block := stream.read(1024 * 1024):
                    archive_digest.update(block)
                with local.open("rb") as local_stream:
                    while block := local_stream.read(1024 * 1024):
                        local_digest.update(block)
                if archive_digest.digest() != local_digest.digest():
                    raise ManagerError(f"extracted file differs from archive: {relative}")
    except (OSError, tarfile.TarError) as exc:
        raise ManagerError(f"cannot verify extracted pack: {exc}") from exc
    actual = {path.relative_to(extracted) for path in extracted.rglob("*") if path != extracted}
    if actual != expected:
        raise ManagerError("extracted pack has missing or unexpected entries")


def run_pack(
    record: PackRecord,
    directory: Path,
    installer_args: Sequence[str] = (),
    *,
    allow_unverified: bool = False,
    runner: Any = subprocess.run,
) -> int:
    if record.error:
        raise ManagerError(record.error)
    if record.archive is not None:
        if record.checksum_state in {"mismatch", "invalid", "unreadable"}:
            raise ManagerError(f"refusing pack with {record.checksum_state} checksum")
        if record.checksum_state != "verified" and not allow_unverified:
            raise ManagerError("pack has no verified checksum; use --allow-unverified explicitly")
    elif not allow_unverified:
        raise ManagerError(
            "extracted pack has no verified archive; use --allow-unverified explicitly"
        )
    extracted = _extract(record, directory)
    if record.archive is not None and record.checksum_state == "verified":
        _verify_extracted(record.archive, extracted, record.key)
    installer = extracted / "install.py"
    if not installer.is_file():
        raise ManagerError(f"pack installer is missing: {installer}")
    command = [sys.executable, str(installer), *installer_args]
    completed = runner(command, cwd=directory, check=False)
    return int(completed.returncode)


def _artifacts(record: PackRecord, directory: Path) -> list[Path]:
    directory = directory.resolve()
    values = [record.archive, record.checksum, record.extracted]
    artifacts: list[Path] = []
    for value in values:
        if value is None or not value.exists():
            continue
        path = value.resolve()
        if path.parent != directory:
            raise ManagerError(f"refusing artifact outside selected directory: {path}")
        artifacts.append(path)
    return artifacts


def quarantine_pack(
    record: PackRecord,
    directory: Path,
    *,
    trash_root: Path | None = None,
) -> Path:
    artifacts = _artifacts(record, directory)
    if not artifacts:
        raise ManagerError("no transfer artifacts remain for this pack")
    root = trash_root or Path("~/.local/share/tool-pack-manager/trash").expanduser()
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = root / f"{stamp}-{record.tool}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(mode=0o700)
    moved: list[dict[str, str]] = []
    try:
        for artifact in artifacts:
            target = destination / artifact.name
            shutil.move(str(artifact), target)
            moved.append({"from": str(artifact), "to": str(target)})
        (destination / "restore.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "pack": record.public(),
                    "moved": moved,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        for item in reversed(moved):
            target = Path(item["to"])
            original = Path(item["from"])
            if target.exists() and not original.exists():
                shutil.move(str(target), original)
        raise
    return destination


def _confirm(prompt: str, input_fn: Any = input) -> bool:
    return input_fn(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def _interactive(directory: Path) -> int:
    while True:
        records = inventory(directory)
        print(f"\nTool packs in {directory}")
        print(_table(records))
        if not records:
            return 0
        selector = input("\nSelect a pack number, or q to quit: ").strip()
        if selector.lower() in {"q", "quit", ""}:
            return 0
        try:
            record = _select(records, selector)
            print(json.dumps(record.public(), indent=2, sort_keys=True))
            action = input("[r]un, [d]elete to recoverable trash, [b]ack, [q]uit: ").strip().lower()
            if action == "r":
                allow = record.checksum_state == "verified" or _confirm(
                    "Checksum is not verified. Run anyway?"
                )
                if allow:
                    result = run_pack(record, directory, allow_unverified=True)
                    print(f"Installer exited {result}")
            elif action == "d":
                artifacts = _artifacts(record, directory)
                print("Will move: " + ", ".join(path.name for path in artifacts))
                if _confirm("Move these transfer artifacts to recoverable trash?"):
                    print(f"Moved to {quarantine_pack(record, directory)}")
            elif action == "q":
                return 0
        except (ManagerError, OSError) as exc:
            print(f"tool-pack-manager: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tool-pack-manager",
        description="Inspect, run, or recoverably remove deployment packs.",
    )
    parser.add_argument(
        "--directory",
        "-C",
        type=Path,
        default=Path.cwd(),
        help="directory to scan (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable inventory")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="list discovered packs")
    run = commands.add_parser("run", help="verify, extract, and run a pack installer")
    run.add_argument("selector", help="list number, bundle ID, directory, or archive name")
    run.add_argument("--allow-unverified", action="store_true")
    run.add_argument("installer_args", nargs=argparse.REMAINDER)
    delete = commands.add_parser("delete", help="move transfer artifacts to recoverable trash")
    delete.add_argument("selector", help="list number, bundle ID, directory, or archive name")
    delete.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    delete.add_argument("--trash-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    directory = args.directory.expanduser().resolve()
    try:
        if args.command is None and not args.json:
            return _interactive(directory)
        records = inventory(directory)
        if args.command in {None, "list"}:
            if args.json:
                print(json.dumps([record.public() for record in records], indent=2, sort_keys=True))
            else:
                print(_table(records))
            return 0
        record = _select(records, args.selector)
        if args.command == "run":
            installer_args = list(args.installer_args)
            if installer_args[:1] == ["--"]:
                installer_args = installer_args[1:]
            return run_pack(
                record,
                directory,
                installer_args,
                allow_unverified=args.allow_unverified,
            )
        artifacts = _artifacts(record, directory)
        if not args.yes:
            print("Will move: " + ", ".join(path.name for path in artifacts))
            if not _confirm("Move these transfer artifacts to recoverable trash?"):
                print("No changes made.")
                return 0
        destination = quarantine_pack(
            record,
            directory,
            trash_root=args.trash_root.expanduser() if args.trash_root else None,
        )
        print(f"Moved to {destination}")
        return 0
    except (ManagerError, OSError) as exc:
        parser.exit(1, f"tool-pack-manager: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
