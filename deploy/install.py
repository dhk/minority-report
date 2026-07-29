#!/usr/bin/env python3
"""Install a pack bundle on Ubuntu without overwriting the prior release.

This file is copied to the root of every archive.  It deliberately uses only
the Python standard library available on a stock Ubuntu host; uv installs the
tool's requested Python and dependencies afterward.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from deploy.checks import (
    print_component_panel,
    required_checks_pass,
    route_paths,
    run_component_checks,
    tailscale_route_state,
)


class InstallError(RuntimeError):
    """The bundle cannot be installed without risking the current release."""


Runner = Callable[[Sequence[str]], None]
_PACK_ROOT_MARKER = ".tool-pack-root.json"
_INSTALL_ROOT_ATTEMPTS = 3
_SUPPORT_MARKER = ".tool-pack-support.json"
_REGISTRY_MARKER = "common-services-registry-format: 1"
_PACK_MANAGER_MARKER = "tool-pack-manager-format: 1"


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("format_version") != 1:
        raise InstallError("unsupported or missing pack manifest")
    for key in ("bundle_id", "tool", "source", "install", "services"):
        if key not in value:
            raise InstallError(f"pack manifest is missing {key!r}")
    return value


def _expanded(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser().resolve()


def _prompt_path(prompt: str, default: Path, input_fn: Callable[[str], str]) -> Path:
    answer = input_fn(f"{prompt} [{default}]: ").strip()
    return _expanded(answer) if answer else default


def resolve_install_root(answer: str, *, bundle_root: Path) -> Path:
    """Validate an operator-supplied install root, or refuse it.

    A relative answer is rejected, never resolved against the working
    directory.  That silent absolutisation is the defect: a password typed at
    the ``Install root`` prompt became a real directory under whichever
    repository the operator happened to be standing in.

    Rejection messages never quote the answer back.  Until it has been proven
    absolute it may not be a path at all, and this runs a line or two above a
    sudo prompt.
    """
    text = answer.strip()
    if not text:
        raise InstallError("install root cannot be empty")
    candidate = Path(os.path.expandvars(text)).expanduser()
    if not candidate.is_absolute():
        raise InstallError("install root must be an absolute path, starting with '/' or '~/'")
    # Only past this point is the answer known to be a path, and so safe to echo.
    resolved = candidate.resolve()
    bundle = bundle_root.resolve()
    if resolved == bundle or bundle in resolved.parents:
        raise InstallError(f"install root must not be inside the bundle: {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise InstallError(f"install root exists but is not a directory: {resolved}")
    if not resolved.parent.is_dir():
        raise InstallError(f"parent directory does not exist: {resolved.parent}")
    return resolved


def prompt_install_root(
    default: Path,
    *,
    bundle_root: Path,
    input_fn: Callable[[str], str] = input,
    stream: TextIO = sys.stdout,
    attempts: int = _INSTALL_ROOT_ATTEMPTS,
) -> Path:
    """Ask for an install root, re-prompting until one validates.

    An unusable answer is refused and asked again rather than turned into a
    directory, and a root that does not exist yet is created only on an
    explicit confirmation.
    """
    print(
        "Install root: the directory this tool is installed into.\n"
        "This asks for a directory, not a password.",
        file=stream,
    )
    for _ in range(attempts):
        answer = input_fn(f"Install root [{default}]: ").strip()
        if not answer:
            return default
        try:
            candidate = resolve_install_root(answer, bundle_root=bundle_root)
        except InstallError as exc:
            print(f"  rejected: {exc}", file=stream)
            continue
        if candidate.exists():
            return candidate
        if _confirm(f"Create new install root {candidate}?", default=False, input_fn=input_fn):
            return candidate
        print("  left alone; nothing was created.", file=stream)
    raise InstallError(
        f"no usable install root after {attempts} attempts; pass --install-root explicitly instead"
    )


def _confirm(prompt: str, *, default: bool, input_fn: Callable[[str], str] = input) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input_fn(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _default_runner(command: Sequence[str]) -> None:
    print(f"→ {shlex.join(command)}", flush=True)
    subprocess.run(list(command), check=True)


def _read_env_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.add(line.split("=", 1)[0].strip())
    return names


def ensure_secrets(
    path: Path,
    required: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    interactive: bool,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> list[str]:
    """Write missing secrets from env or hidden prompts; return unresolved names."""
    environment = os.environ if environ is None else environ
    existing = _read_env_names(path)
    additions: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for name in required:
        if name in existing:
            continue
        value = environment.get(name, "").strip()
        if not value and interactive:
            value = secret_fn(f"{name} (leave blank to configure later): ").strip()
        if not value:
            unresolved.append(name)
            continue
        if "\n" in value or "\r" in value:
            raise InstallError(f"{name} contains a newline and cannot be written safely")
        additions.append((name, value))

    if additions or path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "" if not previous or previous.endswith("\n") else "\n"
        payload = previous + separator + "".join(f"{name}={value}\n" for name, value in additions)
        path.write_text(payload, encoding="utf-8")
        path.chmod(0o600)
    return unresolved


def _environment_line(name: str, value: str) -> str:
    if not name or not name.isascii() or not name.replace("_", "A").isalnum() or name[0].isdigit():
        raise InstallError(f"invalid environment variable name: {name!r}")
    if "\n" in value or "\r" in value:
        raise InstallError(f"{name} contains a newline and cannot be written safely")
    return f"{name}={shlex.quote(value)}"


def render_environment_file(existing: str, managed: Mapping[str, str]) -> str:
    """Update managed entries while preserving comments and unrelated settings."""
    remaining = dict(managed)
    rendered: list[str] = []
    for raw_line in existing.splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            if name in managed:
                if name in remaining:
                    rendered.append(_environment_line(name, remaining.pop(name)))
                continue
        rendered.append(raw_line)
    if remaining and rendered and rendered[-1]:
        rendered.append("")
    rendered.extend(_environment_line(name, remaining[name]) for name in sorted(remaining))
    return "\n".join(rendered).rstrip("\n") + "\n"


def existing_environment_value(path: Path, name: str) -> str | None:
    """Read one setting out of an existing host environment file, if present."""
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("\"'") or None
    return None


def environment_preview(
    managed: Mapping[str, str],
    environment_file: Path,
    *,
    repo_variable: str = "",
    repo_source: str = "",
    install_root: Path | None = None,
) -> list[str]:
    """One line per managed setting: its resolved value, and what changes.

    The dry run exists to catch a wrong data repository before it is installed,
    and it printed every path on the host except that one (#14). Where the
    value came from matters as much as the value: 'preserved from the host
    file' and 'you passed --repo-path' look identical in the result and are
    very different mistakes.
    """
    lines: list[str] = []
    for name in sorted(managed):
        value = managed[name]
        current = existing_environment_value(environment_file, name)
        if current is None:
            change = "new"
        elif current == value:
            change = "unchanged"
        else:
            change = f"CHANGED, was {current}"
        notes = [change]
        if name == repo_variable and repo_source:
            notes.append(repo_source)
        lines.append(f"  {name}: {value}  ({'; '.join(notes)})")
        if name == repo_variable and install_root is not None:
            resolved = Path(value)
            if resolved == install_root or install_root in resolved.parents:
                # The failure #6 fixed, made visible before the install rather
                # than after: a server reading the frozen copy of the data
                # captured inside its own release reports healthy while
                # serving content that can no longer change.
                lines.append(
                    f"    WARNING: this is inside the install root ({install_root}). "
                    "The service would read a frozen copy of the data captured in the "
                    "release and report healthy while serving stale content."
                )
    return lines


def _resolve_external_repo(flag_value: Path | None, existing: str | None) -> Path | None:
    """Pick the data-repository path: explicit flag first, then what the
    operator already configured. Never a default -- see the caller.
    """
    if flag_value is not None:
        return flag_value.expanduser().resolve()
    if existing:
        return Path(existing).expanduser().resolve()
    return None


def ensure_environment_file(path: Path, managed: Mapping[str, str]) -> Path | None:
    """Write canonical host config atomically, backing up any changed file."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    desired = render_environment_file(existing, managed)
    if path.is_file() and existing == desired:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = path.with_name(f"{path.name}.bak-{timestamp}-{uuid.uuid4().hex[:6]}")
        shutil.copy2(path, backup)
        print(f"  preserved previous host config: {backup}")
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(desired, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return backup


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_path(value: str) -> str:
    if not value.startswith("/"):
        raise InstallError(f"systemd path is not absolute: {value}")
    if any(character.isspace() or character in {'"', "'", "\\"} for character in value):
        raise InstallError(f"systemd path contains unsupported quoting characters: {value}")
    return value


def render_service_unit(
    service: Mapping[str, Any],
    *,
    home: Path,
    current: Path,
    environment_file: Path,
    secrets_file: Path,
) -> str:
    entrypoint = str(service["entrypoint"])
    arguments = [str(value) for value in service.get("args", [])]
    executable = home / ".local" / "bin" / entrypoint
    command = " ".join(_systemd_quote(value) for value in [str(executable), *arguments])
    host_environment_file = _systemd_path(str(environment_file))
    secret_environment_file = _systemd_path(str(secrets_file))
    working_directory = _systemd_path(str(current))
    return (
        "[Unit]\n"
        f"Description={service['description']}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile=-{host_environment_file}\n"
        f"EnvironmentFile=-{secret_environment_file}\n"
        f"WorkingDirectory={working_directory}\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_root_needs_adoption(root: Path, tool_name: str) -> bool:
    """Return whether an existing non-empty directory is not pack-managed."""
    if not root.exists():
        return False
    if not root.is_dir():
        raise InstallError(f"install root exists but is not a directory: {root}")
    marker = root / _PACK_ROOT_MARKER
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"install-root marker is unreadable: {marker}") from exc
        if not isinstance(payload, dict) or payload.get("tool") != tool_name:
            raise InstallError(f"install root belongs to another tool: {root}")
        return False
    return any(root.iterdir())


def mark_install_root(root: Path, tool_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _PACK_ROOT_MARKER
    if marker.exists():
        return
    marker.write_text(
        json.dumps({"format_version": 1, "tool": tool_name}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def install_release(source: Path, releases: Path, bundle_id: str, manifest: dict[str, Any]) -> Path:
    release = releases / bundle_id
    if release.exists():
        installed_manifest = release / ".pack-manifest.json"
        if not installed_manifest.is_file():
            raise InstallError(f"existing release has no manifest: {release}")
        existing = load_manifest(installed_manifest)
        if existing.get("bundle_id") != bundle_id:
            raise InstallError(f"existing release identity does not match: {release}")
        return release

    releases.mkdir(parents=True, exist_ok=True)
    staging = releases / f".{bundle_id}.staging-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(source, staging, symlinks=True)
        (staging / ".pack-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(release)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return release


def install_support(
    bundle_root: Path,
    install_root: Path,
    release: Path,
    bundle_id: str,
) -> Path:
    """Preserve the docs launcher without retaining the transferred source copy."""
    support_root = install_root / "support"
    support = support_root / bundle_id
    if support.exists():
        marker = support / _SUPPORT_MARKER
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(
                f"existing support directory is not pack-managed: {support}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("bundle_id") != bundle_id:
            raise InstallError(f"existing support directory identity does not match: {support}")
        return support

    support_root.mkdir(parents=True, exist_ok=True)
    staging = support_root / f".{bundle_id}.staging-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir()
        for filename in (
            "launch-docs.py",
            "manage-packs.py",
            "docs-index.html",
            "pack-manifest.json",
        ):
            source = bundle_root / filename
            if not source.is_file():
                raise InstallError(f"bundle support file is missing: {source}")
            shutil.copy2(source, staging / filename)
        checks_package = bundle_root / "deploy"
        if not checks_package.is_dir():
            raise InstallError(f"bundle checks package is missing: {checks_package}")
        shutil.copytree(checks_package, staging / "deploy")
        (staging / "source").symlink_to(release, target_is_directory=True)
        (staging / _SUPPORT_MARKER).write_text(
            json.dumps({"format_version": 1, "bundle_id": bundle_id}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(support)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return support


def bundle_cleanup_targets(bundle_root: Path, manifest: Mapping[str, Any]) -> list[Path]:
    tool = manifest.get("tool")
    if not isinstance(tool, dict):
        raise InstallError("pack manifest tool section must be an object")
    expected_name = f"{tool.get('name', '')}-{manifest.get('bundle_id', '')}"
    if not expected_name.strip("-") or bundle_root.name != expected_name:
        raise InstallError(
            f"cleanup refused because bundle directory is not named {expected_name!r}: {bundle_root}"
        )
    archive = bundle_root.with_name(f"{bundle_root.name}.tar.gz")
    return [archive, archive.with_suffix(archive.suffix + ".sha256"), bundle_root]


def install_pack_manager(
    bundle_root: Path,
    manifest: Mapping[str, Any],
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    runner: Runner = _default_runner,
) -> Path:
    config = manifest.get("pack_manager")
    if not isinstance(config, dict):
        raise InstallError("pack manifest is missing pack_manager configuration")
    source = bundle_root / "manage-packs.py"
    target = _expanded(str(config.get("install_path", "")))
    if not source.is_file():
        raise InstallError(f"pack manager payload is missing: {source}")
    if not target.is_absolute():
        raise InstallError(f"pack manager install path must be absolute: {target}")
    if target.exists() and not target.is_file():
        raise InstallError(f"refusing non-file pack manager target: {target}")
    desired = source.read_bytes()
    existing = target.read_bytes() if target.is_file() else None
    if existing == desired:
        return target
    managed = existing is None or _PACK_MANAGER_MARKER.encode() in existing[:512]
    privileged = config.get("privileged", False)
    if not isinstance(privileged, bool):
        raise InstallError("pack manager privileged setting must be a boolean")
    if existing is not None:
        if not interactive and not managed:
            raise InstallError(
                f"refusing to replace unknown pack manager non-interactively: {target}"
            )
        if interactive and not _confirm(
            f"Back up and replace pack manager {target}?",
            default=managed,
            input_fn=input_fn,
        ):
            raise InstallError("required pack manager was not installed")
    elif interactive and not _confirm(
        f"Install the pack manager at {target}?",
        default=True,
        input_fn=input_fn,
    ):
        raise InstallError("required pack manager was not installed")

    if privileged:
        runner(["sudo", "install", "-d", "-m", "0755", str(target.parent)])
        if existing is not None:
            timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup = target.with_name(
                f"{target.name}.bak-{timestamp}-{uuid.uuid4().hex[:6]}"
            )
            runner(
                [
                    "sudo",
                    "cp",
                    "--preserve=mode,ownership,timestamps",
                    str(target),
                    str(backup),
                ]
            )
            print(f"  preserved previous pack manager: {backup}")
        runner(
            [
                "sudo",
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                str(source),
                str(target),
            ]
        )
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    if existing is not None:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = target.with_name(f"{target.name}.bak-{timestamp}-{uuid.uuid4().hex[:6]}")
        shutil.copy2(target, backup)
        print(f"  preserved previous pack manager: {backup}")
    staging = target.with_name(f".{target.name}.staging-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copy2(source, staging)
        staging.chmod(0o755)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return target


def cleanup_bundle_artifacts(bundle_root: Path, manifest: Mapping[str, Any]) -> list[Path]:
    """Remove only the exact downloaded archive, checksum, and unpacked bundle."""
    targets = bundle_cleanup_targets(bundle_root, manifest)
    removed: list[Path] = []
    for path in targets[:-1]:
        if not path.exists():
            continue
        if not path.is_file() and not path.is_symlink():
            raise InstallError(f"cleanup target is not a file: {path}")
        path.unlink()
        removed.append(path)
    shutil.rmtree(bundle_root)
    removed.append(bundle_root)
    return removed


def current_release(current: Path) -> Path | None:
    if not current.is_symlink():
        if current.exists():
            raise InstallError(f"{current} exists but is not a managed symlink")
        return None
    target = Path(os.readlink(current))
    return (current.parent / target).resolve() if not target.is_absolute() else target.resolve()


def switch_current(current: Path, release: Path) -> None:
    current.parent.mkdir(parents=True, exist_ok=True)
    temporary = current.with_name(f".{current.name}-{uuid.uuid4().hex[:8]}")
    temporary.symlink_to(release)
    os.replace(temporary, current)


def _find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "uv"
    return str(local) if local.is_file() else None


def _install_uv(runner: Runner) -> str:
    print("Downloading uv's official installer from astral.sh…")
    with tempfile.TemporaryDirectory(prefix="tool-pack-uv-") as temporary:
        installer = Path(temporary) / "install.sh"
        try:
            with urllib.request.urlopen("https://astral.sh/uv/install.sh", timeout=30) as response:
                installer.write_bytes(response.read())
        except (OSError, urllib.error.URLError) as exc:
            raise InstallError(f"could not download uv: {exc}") from exc
        runner(["sh", str(installer)])
    uv = _find_uv()
    if uv is None:
        raise InstallError("uv installation finished but ~/.local/bin/uv was not found")
    return uv


def ensure_uv(*, interactive: bool, input_fn: Callable[[str], str], runner: Runner) -> str:
    uv = _find_uv()
    if uv:
        return uv
    if not interactive or not _confirm(
        "uv is required but not installed. Install it from astral.sh now?",
        default=True,
        input_fn=input_fn,
    ):
        raise InstallError("uv is required; install it from https://docs.astral.sh/uv/")
    return _install_uv(runner)


def _healthy(url: str, expected_service: str, timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                payload = json.loads(response.read(64 * 1024))
                if (
                    200 <= response.status < 300
                    and isinstance(payload, dict)
                    and payload.get("service") == expected_service
                ):
                    return True
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.25)
    return False


@dataclass(frozen=True)
class UnitWrite:
    """One systemd unit this install touched, and how to put it back.

    A failed install used to leave rewritten units behind while reporting a
    clean rollback, so the host ended up in a third state that was neither the
    old install nor the new one (#12). Undoing that needs the displaced
    content, not just the unit's name.
    """

    unit: str
    path: Path
    backup: Path | None = None
    created: bool = False

    @property
    def changed(self) -> bool:
        return self.created or self.backup is not None


def _write_units(
    services: Sequence[Mapping[str, Any]],
    *,
    home: Path,
    current: Path,
    environment_file: Path,
    secrets_file: Path,
) -> list[UnitWrite]:
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    writes: list[UnitWrite] = []
    for service in services:
        unit = str(service["unit"])
        path = unit_dir / unit
        desired = render_service_unit(
            service,
            home=home,
            current=current,
            environment_file=environment_file,
            secrets_file=secrets_file,
        )
        existing = path.read_text(encoding="utf-8") if path.is_file() else None
        if existing == desired:
            writes.append(UnitWrite(unit=unit, path=path))
            continue
        backup: Path | None = None
        if existing is not None:
            timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup = path.with_name(f"{path.name}.bak-{timestamp}-{uuid.uuid4().hex[:6]}")
            shutil.copy2(path, backup)
            print(f"  preserved previous unit: {backup}")
        path.write_text(desired, encoding="utf-8")
        writes.append(
            UnitWrite(unit=unit, path=path, backup=backup, created=existing is None)
        )
    return writes


def restore_units(writes: Sequence[UnitWrite]) -> list[str]:
    """Put displaced unit files back. Returns what could not be restored."""
    failures: list[str] = []
    for write in writes:
        try:
            if write.backup is not None:
                shutil.copy2(write.backup, write.path)
            elif write.created and write.path.exists():
                write.path.unlink()
        except OSError as exc:
            failures.append(f"{write.path} ({exc})")
    return failures


def discard_unit_backups(writes: Sequence[UnitWrite]) -> None:
    """Remove only the backups this run created.

    Earlier runs' backups are left alone: they are somebody else's record of
    somebody else's install, and one of them was how a password typed at the
    wrong prompt survived on disk long after the install that captured it.
    """
    for write in writes:
        if write.backup is None:
            continue
        try:
            write.backup.unlink(missing_ok=True)
        except OSError:
            pass


def _confirm_replacement(
    description: str,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
) -> None:
    if interactive and not _confirm(description, default=False, input_fn=input_fn):
        raise InstallError("aborted before replacing existing installation state")


def _restart_units(units: Sequence[str], runner: Runner) -> None:
    runner(["systemctl", "--user", "daemon-reload"])
    for unit in units:
        runner(["systemctl", "--user", "enable", unit])
        runner(["systemctl", "--user", "restart", unit])


def _offer_linger(*, interactive: bool, input_fn: Callable[[str], str], runner: Runner) -> None:
    if shutil.which("loginctl") is None:
        return
    username = getpass.getuser()
    result = subprocess.run(
        ["loginctl", "show-user", username, "-p", "Linger", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip().lower() == "yes":
        return
    command = ["sudo", "loginctl", "enable-linger", username]
    if interactive and _confirm(
        "Enable systemd user-service linger so the tool survives logout/reboot?",
        default=True,
        input_fn=input_fn,
    ):
        runner(command)
    else:
        print(f"Linger is not enabled. Run later: {shlex.join(command)}")


def _tailscale_dns_name(runner: Callable[..., Any] = subprocess.run) -> str | None:
    try:
        result = runner(
            ["tailscale", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        name = json.loads(result.stdout)["Self"]["DNSName"]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return str(name).rstrip(".") or None


def _registry_base_command(config: Mapping[str, Any]) -> list[str]:
    static_range = config.get("static_range", [])
    dynamic_range = config.get("dynamic_range", [])
    if (
        not isinstance(static_range, list)
        or len(static_range) != 2
        or not isinstance(dynamic_range, list)
        or len(dynamic_range) != 2
    ):
        raise InstallError("registry port ranges are invalid")
    return [
        "sudo",
        str(config["helper_path"]),
        "--registry",
        str(config["data_path"]),
        "--static-range",
        f"{static_range[0]}-{static_range[1]}",
        "--dynamic-range",
        f"{dynamic_range[0]}-{dynamic_range[1]}",
    ]


def _registry_commands(config: Mapping[str, Any]) -> list[list[str]]:
    entries = config.get("entries")
    if not isinstance(entries, list):
        raise InstallError("registry entries are invalid")
    base = _registry_base_command(config)
    commands: list[list[str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise InstallError("registry entry is invalid")
        command = [
            *base,
            "reserve",
            str(entry["service_id"]),
            "--name",
            str(entry["display_name"]),
            "--owner",
            str(entry["owner"]),
            "--protocol",
            str(entry["protocol"]),
            "--address",
            str(entry["address"]),
            "--port",
            str(entry["port"]),
        ]
        for option, key in (
            ("--unit", "unit"),
            ("--health-url", "health_url"),
            ("--health-service", "health_service"),
            ("--source", "source"),
        ):
            if entry.get(key):
                command.extend([option, str(entry[key])])
        if entry.get("adopt_listener") is True:
            command.append("--adopt-listener")
        commands.append(command)
        route = entry.get("route")
        if isinstance(route, dict):
            command = [
                *base,
                "reserve-route",
                str(entry["service_id"]),
                "--host",
                str(route["host"]),
                "--https-port",
                str(route["https_port"]),
            ]
            for path in route_paths(route):
                command.extend(["--path", path])
            command.extend(
                [
                    "--mode",
                    str(route["mode"]),
                    "--target",
                    str(route["target"]),
                ]
            )
            commands.append(command)
    return commands


def _install_registry_helper(
    bundle_root: Path,
    config: Mapping[str, Any],
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    runner: Runner,
) -> None:
    source = bundle_root / "deploy" / "service_registry.py"
    target = Path(str(config["helper_path"]))
    if not source.is_file():
        raise InstallError(f"registry helper payload is missing: {source}")
    if not target.is_absolute():
        raise InstallError(f"registry helper path must be absolute: {target}")
    if target.exists() and not target.is_file():
        raise InstallError(f"refusing non-file registry helper target: {target}")
    desired = source.read_bytes()
    existing = target.read_bytes() if target.is_file() else None
    if existing == desired:
        return
    managed = existing is None or _REGISTRY_MARKER.encode() in existing[:512]
    if existing is not None:
        if not interactive and not managed:
            raise InstallError(
                f"refusing to replace unknown shared registry helper non-interactively: {target}"
            )
        if interactive and not _confirm(
            f"Back up and replace shared registry helper {target}?",
            default=managed,
            input_fn=input_fn,
        ):
            raise InstallError("required registry helper was not installed")
    elif interactive and not _confirm(
        f"Install shared host service registry helper at {target}?",
        default=True,
        input_fn=input_fn,
    ):
        raise InstallError("required registry helper was not installed")

    runner(["sudo", "install", "-d", "-m", "0755", str(target.parent)])
    if existing is not None:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = target.with_name(f"{target.name}.bak-{timestamp}-{uuid.uuid4().hex[:6]}")
        runner(["sudo", "cp", "--preserve=mode,ownership,timestamps", str(target), str(backup)])
        print(f"  preserved previous registry helper: {backup}")
    runner(
        [
            "sudo",
            "install",
            "-o",
            "root",
            "-g",
            "root",
            "-m",
            "0755",
            str(source),
            str(target),
        ]
    )


def _configure_registry(
    bundle_root: Path,
    manifest: Mapping[str, Any],
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    runner: Runner,
) -> None:
    raw = manifest.get("registry")
    if not isinstance(raw, dict):
        return
    _install_registry_helper(
        bundle_root,
        raw,
        interactive=interactive,
        input_fn=input_fn,
        runner=runner,
    )
    for command in _registry_commands(raw):
        runner(command)


def _tailscale_command(config: Mapping[str, Any]) -> list[str]:
    mode = str(config["mode"])
    port = int(config.get("port", 443))
    command = ["sudo", "tailscale", mode]
    if port != 443:
        command.append(f"--https={port}")
    command.extend(["--yes", "--bg", "--set-path", str(config["path"]), str(config["target"])])
    return command


def _configure_tailscale(
    manifest: Mapping[str, Any],
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    runner: Runner,
) -> None:
    raw = manifest.get("tailscale")
    if not isinstance(raw, dict):
        return
    required = bool(raw.get("required", False))
    state, detail = tailscale_route_state(raw)
    if state == "pass":
        print(f"Tailscale route already configured: {detail}")
        return
    if state == "unavailable":
        if required:
            raise InstallError(detail)
        print(f"WARNING: {detail}", file=sys.stderr)
        return

    command = _tailscale_command(raw)
    if state == "conflict":
        if not interactive:
            raise InstallError(
                f"refusing to replace an existing Tailscale route non-interactively: {detail}"
            )
        if not _confirm(
            f"{detail}. Replace only this path mapping?",
            default=False,
            input_fn=input_fn,
        ):
            if required:
                raise InstallError("required Tailscale route was not configured")
            return
    elif interactive and not _confirm(
        f"Add {raw['mode']} route {raw['path']} → {raw['target']}?",
        default=True,
        input_fn=input_fn,
    ):
        if required:
            raise InstallError("required Tailscale route was not configured")
        return

    runner(command)
    state, detail = tailscale_route_state(raw)
    if state != "pass":
        raise InstallError(f"Tailscale route did not become ready: {detail}")
    print(f"Tailscale route ready: {detail}")


def _print_capability(
    manifest: Mapping[str, Any],
    *,
    tailscale_dns: Callable[[], str | None] = _tailscale_dns_name,
) -> None:
    raw = manifest.get("capability")
    if not isinstance(raw, dict):
        return
    token_file = _expanded(str(raw.get("token_file", "")))
    if not token_file.is_file():
        print(f"Capability token not found yet at {token_file}")
        return
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        return
    dns_name = tailscale_dns()
    print("\nCapability URLs (treat these like passwords):")
    for template in raw.get("urls", []):
        rendered = str(template).replace("{token}", token)
        if "{tailscale_dns}" in rendered:
            if not dns_name:
                continue
            rendered = rendered.replace("{tailscale_dns}", dns_name)
        print(f"  {rendered}")
    if any("{tailscale_dns}" in str(template) for template in raw.get("urls", [])) and not dns_name:
        print("  (Tailscale DNS name unavailable; tunnel URL omitted.)")


def export_constraints(
    release: Path,
    *,
    uv: str,
    runner: Callable[..., Any] = subprocess.run,
) -> Path | None:
    """Pin the tool install to the versions the lockfile records.

    ``uv tool install`` resolves dependencies fresh and ignores ``uv.lock``,
    so the deployed service can run versions nobody tested while CI and every
    local run use the locked ones. Observed as a ``pydantic-settings`` 2.15
    warning on a host whose lock pins 2.14.2 — harmless in itself, but it is
    the visible edge of "deployed is not what was tested", which is the same
    silent divergence this pack keeps being bitten by.

    Returns None when the release ships no lockfile or the export fails: the
    pack format is project-agnostic, a lock is not guaranteed, and an install
    that resolves freely is still better than no install.
    """
    if not (release / "uv.lock").is_file():
        return None
    try:
        result = runner(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--directory",
                str(release),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0 or not (result.stdout or "").strip():
        return None
    target = release / ".tool-constraints.txt"
    try:
        target.write_text(result.stdout, encoding="utf-8")
    except OSError:
        return None
    return target


def tool_install_command(uv: str, release: Path, constraints: Path | None) -> list[str]:
    """The uv invocation that installs a release, locked where possible."""
    command = [uv, "tool", "install", "--reinstall"]
    if constraints is not None:
        command.extend(["--constraints", str(constraints)])
    command.append(str(release))
    return command


def _report_rollback(reverted: Sequence[str], survived: Sequence[str]) -> None:
    """Say what came back and what did not.

    "The install failed" and "the host is unchanged" are different statements,
    and reporting the first while meaning something between the two is what
    made a failed install expensive to diagnose (#12).
    """
    if reverted:
        print("Reverted: " + ", ".join(reverted) + ".", file=sys.stderr)
    if not survived:
        print("The host is back to its previous state.", file=sys.stderr)
        return
    print(
        "NOT reverted — these host changes remain in place:",
        file=sys.stderr,
    )
    for item in survived:
        print(f"  - {item}", file=sys.stderr)


def _rollback(
    previous: Path | None,
    *,
    current: Path,
    uv: str,
    units: Sequence[UnitWrite],
    runner: Runner,
    survived: Sequence[str] = (),
) -> None:
    changed = [write for write in units if write.changed]
    remaining = list(survived)
    if previous is None or not previous.is_dir():
        print("No earlier release was available for automatic rollback.", file=sys.stderr)
        if changed:
            remaining.insert(
                0, "systemd units were rewritten: " + ", ".join(write.unit for write in changed)
            )
        _report_rollback([], remaining)
        return
    print(f"Rolling back to {previous.name}…", file=sys.stderr)
    reverted: list[str] = []
    switch_current(current, previous)
    reverted.append("release symlink")
    runner(tool_install_command(uv, previous, export_constraints(previous, uv=uv)))
    reverted.append("installed commands")
    if changed:
        failures = restore_units(changed)
        if failures:
            remaining.insert(0, "systemd units could not be restored: " + ", ".join(failures))
        else:
            reverted.append("systemd units")
            discard_unit_backups(changed)
    _restart_units([write.unit for write in units], runner)
    _report_rollback(reverted, remaining)


def main(argv: list[str] | None = None) -> int:
    bundle_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install or upgrade this tool bundle with recoverable releases.",
    )
    parser.add_argument("--install-root", type=Path)
    parser.add_argument(
        "--repo-path",
        type=Path,
        help="Path to the data repository this service reads, for packs whose data "
        "lives outside the install root. Preserved across upgrades once set.",
    )
    parser.add_argument("--yes", action="store_true", help="accept defaults; never prompt")
    parser.add_argument("--skip-service", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--keep-bundle",
        action="store_true",
        help="retain the transferred archive, checksum, and unpacked bundle after success",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the installed component checks without changing anything",
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(bundle_root / "pack-manifest.json")
        install = manifest["install"]
        tool = manifest["tool"]
        if not isinstance(install, dict) or not isinstance(tool, dict):
            raise InstallError("pack manifest install/tool sections must be objects")
        default_root = _expanded(str(install["default_root"]))
        if args.install_root:
            install_root = resolve_install_root(str(args.install_root), bundle_root=bundle_root)
        elif args.yes or args.dry_run:
            install_root = default_root
        else:
            install_root = prompt_install_root(default_root, bundle_root=bundle_root)
        releases = install_root / "releases"
        current = install_root / "current"
        source = bundle_root / "source"
        environment_file = _expanded(str(install["environment_file"]))
        secrets_file = _expanded(str(install["secrets_file"]))
        raw_environment = install.get("environment", {})
        if not isinstance(raw_environment, dict) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in raw_environment.items()
        ):
            raise InstallError("pack manifest install.environment must map names to strings")
        managed_environment = {
            name: str(_expanded(value)) if value.startswith(("~", "/")) else value
            for name, value in raw_environment.items()
        }
        repo_variable = str(install["repo_environment"])
        if bool(install.get("repo_is_install_root", True)):
            managed_environment[repo_variable] = str(current)
            repo_source = "from this pack's install root"
        else:
            # The code and the data it reads are different repositories, so the
            # install root says nothing about where the data lives. Pointing
            # this at ``current`` would aim it at a frozen copy of the data
            # captured inside the release -- which reads as a working server
            # serving silently stale content, the worst available failure.
            configured_repo = existing_environment_value(environment_file, repo_variable)
            resolved_repo = _resolve_external_repo(args.repo_path, configured_repo)
            repo_source = (
                "from --repo-path"
                if args.repo_path is not None
                else f"preserved from {environment_file}"
            )
            if resolved_repo is None:
                raise InstallError(
                    f"This pack keeps {repo_variable} outside the install root, and no existing "
                    f"value was found in {environment_file}. Re-run with "
                    f"--repo-path /path/to/checkout. Refusing to guess: a wrong value here "
                    f"produces a healthy server serving stale data."
                )
            managed_environment[repo_variable] = str(resolved_repo)
        services = manifest["services"]
        if not isinstance(services, list) or not all(isinstance(item, dict) for item in services):
            raise InstallError("pack manifest services must be an array of objects")
        if args.check:
            checks = run_component_checks(
                manifest,
                bundle_root,
                install_root=install_root,
                include_services=not args.skip_service,
            )
            print_component_panel(checks)
            return 0 if required_checks_pass(checks) else 1
        tool_name = str(tool["name"])
        adoption_needed = install_root_needs_adoption(install_root, tool_name)
        previous = current_release(current)
        release_path = releases / str(manifest["bundle_id"])
        command_paths = sorted(
            {Path.home() / ".local" / "bin" / str(service["entrypoint"]) for service in services},
            key=str,
        )
        existing_commands = [path for path in command_paths if path.exists() or path.is_symlink()]
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        if environment_file.exists() and not environment_file.is_file():
            raise InstallError(f"host environment path is not a regular file: {environment_file}")
        existing_environment = (
            environment_file.read_text(encoding="utf-8") if environment_file.is_file() else ""
        )
        environment_needs_update = (
            not environment_file.is_file()
            or render_environment_file(existing_environment, managed_environment)
            != existing_environment
        )
        differing_units: list[Path] = []
        for service in services:
            unit_path = unit_dir / str(service["unit"])
            if unit_path.exists() and not unit_path.is_file():
                raise InstallError(f"systemd unit path is not a regular file: {unit_path}")
            desired = render_service_unit(
                service,
                home=Path.home(),
                current=current,
                environment_file=environment_file,
                secrets_file=secrets_file,
            )
            if unit_path.is_file() and unit_path.read_text(encoding="utf-8") != desired:
                differing_units.append(unit_path)

        if args.dry_run:
            print(f"Bundle: {manifest['bundle_id']}")
            print(f"Source: {source}")
            print(f"New release: {release_path}")
            print(
                f"Install root: {install_root} ({'needs adoption' if adoption_needed else 'available'})"
            )
            print(f"Current link: {current} -> {previous or 'not installed'}")
            print(
                f"Host config: {environment_file} "
                f"({'requires update' if environment_needs_update else 'current'})"
            )
            for line in environment_preview(
                managed_environment,
                environment_file,
                repo_variable=repo_variable,
                repo_source=repo_source,
                install_root=install_root,
            ):
                print(line)
            print(f"Secrets: {secrets_file}")
            print("Services: " + ", ".join(str(item["unit"]) for item in services))
            print(
                "Existing commands: "
                + (", ".join(str(path) for path in existing_commands) or "none")
            )
            print(
                "Systemd units requiring backup/replacement: "
                + (", ".join(str(path) for path in differing_units) or "none")
            )
            tailscale = manifest.get("tailscale")
            if isinstance(tailscale, dict):
                print("Tailscale route: " + shlex.join(_tailscale_command(tailscale)))
            registry = manifest.get("registry")
            if isinstance(registry, dict):
                print(f"Service registry: {registry.get('data_path')}")
                for command in _registry_commands(registry):
                    print("  " + shlex.join(command))
            pack_manager = manifest.get("pack_manager")
            if isinstance(pack_manager, dict):
                print(f"Pack manager: {_expanded(str(pack_manager.get('install_path', '')))}")
            if args.keep_bundle:
                print("Post-install cleanup: disabled by --keep-bundle")
            else:
                print(
                    "Post-install cleanup: "
                    + ", ".join(str(path) for path in bundle_cleanup_targets(bundle_root, manifest))
                )
            return 0

        if sys.platform != "linux" and not args.skip_service:
            raise InstallError("service installation requires Linux; use --skip-service elsewhere")
        if not source.is_dir():
            raise InstallError(f"bundle source directory is missing: {source}")

        interactive = not args.yes
        if adoption_needed:
            if not interactive:
                raise InstallError(
                    f"{install_root} contains files from outside the pack installer; "
                    "rerun interactively to inspect and adopt it, or choose another --install-root"
                )
            _confirm_replacement(
                f"{install_root} already contains files. Adopt it without deleting anything?",
                interactive=True,
                input_fn=input,
            )
        if previous is not None and previous != release_path:
            _confirm_replacement(
                f"Switch {current} from {previous} to {release_path}? The old release is retained.",
                interactive=interactive,
                input_fn=input,
            )
        if existing_commands:
            _confirm_replacement(
                "uv will reinstall and replace these existing command links: "
                + ", ".join(str(path) for path in existing_commands)
                + ". Continue?",
                interactive=interactive,
                input_fn=input,
            )
        for unit_path in differing_units:
            _confirm_replacement(
                f"Back up and replace the differing systemd unit {unit_path}?",
                interactive=interactive,
                input_fn=input,
            )
        if environment_file.is_file() and environment_needs_update:
            _confirm_replacement(
                f"Back up and update the managed settings in {environment_file}?",
                interactive=interactive,
                input_fn=input,
            )

        mark_install_root(install_root, tool_name)
        release = install_release(source, releases, str(manifest["bundle_id"]), manifest)
        support = install_support(
            bundle_root,
            install_root,
            release,
            str(manifest["bundle_id"]),
        )
        pack_manager_path = install_pack_manager(
            bundle_root,
            manifest,
            interactive=interactive,
            input_fn=input,
            runner=_default_runner,
        )
        if manifest.get("source", {}).get("dirty"):
            print("WARNING: this bundle contains uncommitted source changes.")
        uv = ensure_uv(interactive=interactive, input_fn=input, runner=_default_runner)
        constraints = export_constraints(release, uv=uv)
        if constraints is None:
            print(
                "  (no lockfile in this release — dependencies resolve freely, so the "
                "installed versions may differ from the tested ones)"
            )
        _default_runner(tool_install_command(uv, release, constraints))

        required = install.get("required_secrets", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise InstallError("required_secrets must be an array of names")
        unresolved = ensure_secrets(
            secrets_file,
            required,
            interactive=interactive,
        )
        if unresolved:
            print("Secrets still required before research can run: " + ", ".join(unresolved))

        ensure_environment_file(environment_file, managed_environment)
        switch_current(current, release)
        units: list[UnitWrite] = []
        # Host-level changes an automatic rollback cannot undo: both need root,
        # and both are shared with other services, so guessing at a revert is
        # worse than saying plainly that they survived (#12).
        survived: list[str] = []
        if not args.skip_service:
            if shutil.which("systemctl") is None:
                raise InstallError(
                    "systemctl is unavailable; use --skip-service for a tool-only install"
                )
            try:
                _configure_registry(
                    bundle_root,
                    manifest,
                    interactive=interactive,
                    input_fn=input,
                    runner=_default_runner,
                )
                if isinstance(manifest.get("registry"), dict):
                    survived.append(
                        "service registry reservations in "
                        f"{manifest['registry'].get('data_path', 'the host registry')}"
                    )
                units = _write_units(
                    services,
                    home=Path.home(),
                    current=current,
                    environment_file=environment_file,
                    secrets_file=secrets_file,
                )
                _restart_units([write.unit for write in units], _default_runner)
                failed = [
                    str(service["health_url"])
                    for service in services
                    if not _healthy(
                        str(service["health_url"]),
                        str(service["health_service"]),
                    )
                ]
                if failed:
                    raise InstallError("health checks failed: " + ", ".join(failed))
                _configure_tailscale(
                    manifest,
                    interactive=interactive,
                    input_fn=input,
                    runner=_default_runner,
                )
                if isinstance(manifest.get("tailscale"), dict):
                    survived.append(
                        f"tailscale {manifest['tailscale'].get('mode', 'serve')} route "
                        f"{manifest['tailscale'].get('path', '')} -> "
                        f"{manifest['tailscale'].get('target', '')}"
                    )
            except (OSError, InstallError, subprocess.CalledProcessError):
                _rollback(
                    previous,
                    current=current,
                    uv=uv,
                    units=units,
                    runner=_default_runner,
                    survived=survived,
                )
                raise
        checks = run_component_checks(
            manifest,
            bundle_root,
            install_root=install_root,
            include_services=not args.skip_service,
        )
        print_component_panel(checks)
        if not required_checks_pass(checks):
            if units:
                _rollback(
                    previous,
                    current=current,
                    uv=uv,
                    units=units,
                    runner=_default_runner,
                    survived=survived,
                )
            raise InstallError("one or more required component checks failed")
        if units:
            # The install stuck, so the displaced units are no longer rollback
            # material -- and an uncollected backup is how a mistyped password
            # outlived the install that captured it (#11/#12).
            discard_unit_backups(units)
            _offer_linger(interactive=interactive, input_fn=input, runner=_default_runner)

        print(f"\nInstalled {tool['display_name']} {tool['version']}")
        print(f"Current release: {current} -> {release}")
        print("Older releases remain under " + str(releases))
        print(f"Installed documentation: {support / 'launch-docs.py'}")
        print(f"Installed pack manager: {pack_manager_path}")
        _print_capability(manifest)
        cleanup = not args.keep_bundle and (
            args.yes
            or _confirm(
                "Remove the transferred archive, checksum, and unpacked bundle now?",
                default=True,
                input_fn=input,
            )
        )
        if cleanup:
            try:
                removed = cleanup_bundle_artifacts(bundle_root, manifest)
                print("Removed transfer artifacts: " + ", ".join(str(path) for path in removed))
                print(f"Documentation remains installed at {support / 'launch-docs.py'}")
            except (OSError, InstallError) as exc:
                print(
                    f"WARNING: installation succeeded but cleanup did not: {exc}", file=sys.stderr
                )
        return 0
    except (KeyError, OSError, InstallError, subprocess.CalledProcessError) as exc:
        print(f"install: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
