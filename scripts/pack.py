#!/usr/bin/env python3
"""Build a self-installing, secret-free deployment archive for an Ubuntu host.

The builder is intentionally project-agnostic.  A repository supplies
``deploy/pack.toml`` plus the stdlib-only ``deploy/install.py`` payload.  Files
come from ``git ls-files`` (tracked files, working-tree edits, and non-ignored
untracked files), so ignored caches and secrets never enter the bundle.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import html
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


class PackError(RuntimeError):
    """The repository cannot be packaged safely."""


@dataclass(frozen=True)
class ServiceSpec:
    unit: str
    description: str
    entrypoint: str
    args: list[str]
    health_url: str
    health_service: str


@dataclass(frozen=True)
class CapabilitySpec:
    token_file: str
    urls: list[str]


@dataclass(frozen=True)
class TailscaleSpec:
    mode: str
    path: str
    target: str
    port: int
    required: bool


@dataclass(frozen=True)
class RegistryRouteSpec:
    mode: str
    host: str
    https_port: int
    path: str
    target: str


@dataclass(frozen=True)
class RegistryEntrySpec:
    service_id: str
    display_name: str
    owner: str
    protocol: str
    address: str
    port: int
    unit: str | None
    health_url: str | None
    health_service: str | None
    source: str | None
    adopt_listener: bool
    route: RegistryRouteSpec | None


@dataclass(frozen=True)
class RegistrySpec:
    helper_path: str
    data_path: str
    static_range: list[int]
    dynamic_range: list[int]
    required: bool
    entries: list[RegistryEntrySpec]


@dataclass(frozen=True)
class PackSpec:
    format_version: int
    name: str
    display_name: str
    default_install_root: str
    repo_environment: str
    environment_file: str
    environment: dict[str, str]
    secrets_file: str
    required_secrets: list[str]
    exclude: list[str]
    services: list[ServiceSpec]
    capability: CapabilitySpec | None
    tailscale: TailscaleSpec | None
    registry: RegistrySpec | None


@dataclass(frozen=True)
class BuildResult:
    archive: Path
    checksum: Path
    bundle_root: str
    file_count: int
    byte_count: int


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"deploy/pack.toml requires a non-empty {key!r} value")
    return value.strip()


def _string_list(mapping: dict[str, Any], key: str) -> list[str]:
    value = mapping.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PackError(f"deploy/pack.toml {key!r} must be an array of strings")
    return [item for item in value if item]


def _string_mapping(mapping: dict[str, Any], key: str) -> dict[str, str]:
    value = mapping.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and name and isinstance(item, str) and item
        for name, item in value.items()
    ):
        raise PackError(f"deploy/pack.toml {key!r} must be a string-to-string table")
    return dict(value)


def _optional_string(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"deploy/pack.toml {key!r} must be a non-empty string when present")
    return value.strip()


def _port_range(mapping: dict[str, Any], key: str) -> list[int]:
    value = mapping.get(key)
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        or not 1024 <= value[0] <= value[1] <= 49151
    ):
        raise PackError(f"deploy/pack.toml {key!r} must be [START, END] within 1024-49151")
    return list(value)


def load_spec(path: Path) -> PackSpec:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    pack = raw.get("pack")
    if not isinstance(pack, dict):
        raise PackError("deploy/pack.toml requires a [pack] table")
    version = pack.get("format_version")
    if version != 1:
        raise PackError(f"unsupported pack format {version!r}; expected 1")

    raw_services = raw.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise PackError("deploy/pack.toml requires at least one [[services]] table")
    services: list[ServiceSpec] = []
    for raw_service in raw_services:
        if not isinstance(raw_service, dict):
            raise PackError("every [[services]] entry must be a table")
        services.append(
            ServiceSpec(
                unit=_required_string(raw_service, "unit"),
                description=_required_string(raw_service, "description"),
                entrypoint=_required_string(raw_service, "entrypoint"),
                args=_string_list(raw_service, "args"),
                health_url=_required_string(raw_service, "health_url"),
                health_service=_required_string(raw_service, "health_service"),
            )
        )

    raw_capability = raw.get("capability")
    capability = None
    if raw_capability is not None:
        if not isinstance(raw_capability, dict):
            raise PackError("[capability] must be a table")
        capability = CapabilitySpec(
            token_file=_required_string(raw_capability, "token_file"),
            urls=_string_list(raw_capability, "urls"),
        )

    raw_tailscale = raw.get("tailscale")
    tailscale = None
    if raw_tailscale is not None:
        if not isinstance(raw_tailscale, dict):
            raise PackError("[tailscale] must be a table")
        mode = _required_string(raw_tailscale, "mode")
        if mode not in {"serve", "funnel"}:
            raise PackError("deploy/pack.toml [tailscale].mode must be 'serve' or 'funnel'")
        path_value = _required_string(raw_tailscale, "path")
        if not path_value.startswith("/") or path_value == "/":
            raise PackError("deploy/pack.toml [tailscale].path must be a non-root /path")
        port = raw_tailscale.get("port", 443)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise PackError("deploy/pack.toml [tailscale].port must be an integer from 1 to 65535")
        required = raw_tailscale.get("required", False)
        if not isinstance(required, bool):
            raise PackError("deploy/pack.toml [tailscale].required must be a boolean")
        tailscale = TailscaleSpec(
            mode=mode,
            path=path_value.rstrip("/"),
            target=_required_string(raw_tailscale, "target"),
            port=port,
            required=required,
        )

    raw_registry = raw.get("registry")
    registry = None
    if raw_registry is not None:
        if not isinstance(raw_registry, dict):
            raise PackError("[registry] must be a table")
        required = raw_registry.get("required", False)
        if not isinstance(required, bool):
            raise PackError("deploy/pack.toml [registry].required must be a boolean")
        raw_entries = raw_registry.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise PackError("deploy/pack.toml requires at least one [[registry.entries]] table")
        entries: list[RegistryEntrySpec] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise PackError("every [[registry.entries]] entry must be a table")
            protocol = str(raw_entry.get("protocol", "tcp"))
            if protocol != "tcp":
                raise PackError("registry format v1 supports protocol = 'tcp' only")
            port = raw_entry.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 49151:
                raise PackError("every [[registry.entries]] port must be within 1024-49151")
            health_url = _optional_string(raw_entry, "health_url")
            health_service = _optional_string(raw_entry, "health_service")
            if bool(health_url) != bool(health_service):
                raise PackError("registry health_url and health_service must be supplied together")
            route_keys = {
                "route_mode",
                "route_host",
                "route_https_port",
                "route_path",
                "route_target",
            }
            present_route_keys = route_keys.intersection(raw_entry)
            route = None
            if present_route_keys:
                if present_route_keys != route_keys:
                    missing = ", ".join(sorted(route_keys - present_route_keys))
                    raise PackError(f"registry route is incomplete; missing {missing}")
                route_mode = _required_string(raw_entry, "route_mode")
                if route_mode not in {"serve", "funnel"}:
                    raise PackError("registry route_mode must be 'serve' or 'funnel'")
                route_port = raw_entry["route_https_port"]
                if (
                    not isinstance(route_port, int)
                    or isinstance(route_port, bool)
                    or not 1 <= route_port <= 65535
                ):
                    raise PackError("registry route_https_port must be within 1-65535")
                route_path = _required_string(raw_entry, "route_path")
                if not route_path.startswith("/"):
                    raise PackError("registry route_path must start with '/'")
                route = RegistryRouteSpec(
                    mode=route_mode,
                    host=_required_string(raw_entry, "route_host"),
                    https_port=route_port,
                    path=route_path.rstrip("/") or "/",
                    target=_required_string(raw_entry, "route_target"),
                )
            adopt_listener = raw_entry.get("adopt_listener", False)
            if not isinstance(adopt_listener, bool):
                raise PackError("registry adopt_listener must be a boolean")
            entries.append(
                RegistryEntrySpec(
                    service_id=_required_string(raw_entry, "service_id"),
                    display_name=_required_string(raw_entry, "display_name"),
                    owner=_required_string(raw_entry, "owner"),
                    protocol=protocol,
                    address=_required_string(raw_entry, "address"),
                    port=port,
                    unit=_optional_string(raw_entry, "unit"),
                    health_url=health_url,
                    health_service=health_service,
                    source=_optional_string(raw_entry, "source"),
                    adopt_listener=adopt_listener,
                    route=route,
                )
            )
        static_range = _port_range(raw_registry, "static_range")
        dynamic_range = _port_range(raw_registry, "dynamic_range")
        if max(static_range[0], dynamic_range[0]) <= min(static_range[1], dynamic_range[1]):
            raise PackError("registry static_range and dynamic_range must not overlap")
        outside_static = [
            entry.service_id
            for entry in entries
            if not static_range[0] <= entry.port <= static_range[1]
        ]
        if outside_static:
            raise PackError(
                "registry static entries outside static_range: " + ", ".join(outside_static)
            )
        registry = RegistrySpec(
            helper_path=_required_string(raw_registry, "helper_path"),
            data_path=_required_string(raw_registry, "data_path"),
            static_range=static_range,
            dynamic_range=dynamic_range,
            required=required,
            entries=entries,
        )

    return PackSpec(
        format_version=1,
        name=_required_string(pack, "name"),
        display_name=_required_string(pack, "display_name"),
        default_install_root=_required_string(pack, "default_install_root"),
        repo_environment=_required_string(pack, "repo_environment"),
        environment_file=_required_string(pack, "environment_file"),
        environment=_string_mapping(pack, "environment"),
        secrets_file=_required_string(pack, "secrets_file"),
        required_secrets=_string_list(pack, "required_secrets"),
        exclude=_string_list(pack, "exclude"),
        services=services,
        capability=capability,
        tailscale=tailscale,
        registry=registry,
    )


def _git(root: Path, *args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode and not allow_failure:
        detail = result.stderr.strip() or "git command failed"
        raise PackError(detail)
    return result.stdout.strip() if result.returncode == 0 else ""


def _always_excluded(relative: Path) -> bool:
    parts = set(relative.parts)
    if parts & {".git", ".scratch", ".venv", "__pycache__", "dist"}:
        return True
    if relative.name in {"secrets.env", "keys.env", "mcp-http-token", "installations-token"}:
        return True
    if relative.name == ".env":
        return True
    return relative.suffix.lower() in {".pem", ".p12", ".pfx"}


def source_files(root: Path, exclude: list[str]) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    root_resolved = root.resolve()
    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        relative_text = relative.as_posix()
        if _always_excluded(relative) or any(
            fnmatch.fnmatch(relative_text, pattern) for pattern in exclude
        ):
            continue
        path = root / relative
        if not path.is_file() and not path.is_symlink():
            continue
        if path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(root_resolved)
            except (FileNotFoundError, ValueError) as exc:
                raise PackError(f"refusing external or broken symlink: {relative_text}") from exc
        files.append(relative)
    return sorted(files, key=lambda item: item.as_posix())


def _project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream).get("project")
    if not isinstance(project, dict):
        raise PackError("pyproject.toml requires a [project] table")
    return _required_string(project, "version")


def _manifest(root: Path, spec: PackSpec, bundle_id: str) -> dict[str, Any]:
    remote = _git(root, "remote", "get-url", "origin", allow_failure=True) or None
    status = _git(root, "status", "--porcelain=v1")
    return {
        "format_version": spec.format_version,
        "bundle_id": bundle_id,
        "built_at": datetime.now(UTC).isoformat(),
        "tool": {
            "name": spec.name,
            "display_name": spec.display_name,
            "version": _project_version(root),
        },
        "source": {
            "commit": _git(root, "rev-parse", "HEAD"),
            "dirty": bool(status),
            "remote": remote,
        },
        "install": {
            "default_root": spec.default_install_root,
            "repo_environment": spec.repo_environment,
            "environment_file": spec.environment_file,
            "environment": spec.environment,
            "secrets_file": spec.secrets_file,
            "required_secrets": spec.required_secrets,
        },
        "services": [asdict(service) for service in spec.services],
        "capability": asdict(spec.capability) if spec.capability else None,
        "tailscale": asdict(spec.tailscale) if spec.tailscale else None,
        "registry": asdict(spec.registry) if spec.registry else None,
    }


def _copy_source(root: Path, destination: Path, files: list[Path]) -> int:
    total = 0
    for relative in files:
        source = root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
            total += source.lstat().st_size
        else:
            shutil.copy2(source, target)
            total += source.stat().st_size
    return total


def _large_files(root: Path, files: list[Path], max_file_bytes: int) -> list[Path]:
    return [
        relative
        for relative in files
        if not (root / relative).is_symlink() and (root / relative).stat().st_size > max_file_bytes
    ]


def _docs_index(manifest: dict[str, Any], files: list[Path]) -> str:
    documents = [
        relative
        for relative in files
        if relative.suffix.lower() in {".md", ".html", ".htm"}
        and (relative.name.lower().startswith("readme") or "docs" in relative.parts)
    ]
    documents.sort(
        key=lambda relative: (
            0 if relative == Path("README.md") else 1,
            relative.as_posix().lower(),
        )
    )
    rows = []
    for relative in documents:
        label = html.escape(relative.as_posix())
        href = quote(f"source/{relative.as_posix()}", safe="/")
        kind = "Rendered HTML" if relative.suffix.lower() in {".html", ".htm"} else "Markdown"
        rows.append(f'<li><a href="{href}">{label}</a><span>{kind}</span></li>')
    tool = manifest["tool"]
    source = manifest["source"]
    dirty = " · working-tree build" if source["dirty"] else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(str(tool["display_name"]))} documentation</title>
<style>
:root{{color-scheme:light dark}}body{{font:16px/1.55 system-ui;margin:0;background:#f7f7f4;color:#202124}}
main{{max-width:900px;margin:auto;padding:64px 28px}}h1{{font-size:42px;letter-spacing:-.03em;margin-bottom:8px}}
p{{color:#62656b}}ul{{list-style:none;padding:0;border-top:1px solid #ccd0d5}}
li{{display:flex;justify-content:space-between;gap:24px;padding:13px 0;border-bottom:1px solid #ccd0d5}}
a{{color:#3155c6;text-decoration:none;font-weight:600}}span{{color:#777;font-size:13px;text-transform:uppercase}}
.front-panel{{border:1px solid #ccd0d5;border-radius:6px;margin:32px 0;background:#fff}}
.front-panel summary{{cursor:pointer;padding:16px;font-weight:700}}.panel-body{{padding:0 16px 16px}}
.component{{display:grid;grid-template-columns:12px 180px 1fr;gap:12px;align-items:start;padding:9px 0;border-top:1px solid #e5e7ea}}
.lamp{{width:10px;height:10px;margin-top:5px;border-radius:50%;background:#a7abb2;box-shadow:0 0 0 3px rgba(167,171,178,.14)}}
.component.pass .lamp{{background:#16836f;box-shadow:0 0 0 3px rgba(22,131,111,.14)}}
.component.fail .lamp{{background:#c45723;box-shadow:0 0 0 3px rgba(196,87,35,.14)}}
.component.skip .lamp{{background:#ad8120;box-shadow:0 0 0 3px rgba(173,129,32,.14)}}
.component strong{{font-size:14px}}.component small{{color:#777;overflow-wrap:anywhere}}
button{{border:1px solid #3155c6;background:#3155c6;color:white;padding:9px 13px;border-radius:4px;cursor:pointer}}
.panel-note{{font-size:13px}}@media(prefers-color-scheme:dark){{body{{background:#17181a;color:#eee}}p,span,.component small{{color:#aaa}}ul,li,.front-panel,.component{{border-color:#3b3e43}}.front-panel{{background:#202225}}}}
</style></head><body><main>
<p>Deployment pack</p><h1>{html.escape(str(tool["display_name"]))}</h1>
<p>Version {html.escape(str(tool["version"]))} · commit {html.escape(str(source["commit"]))[:12]}{dirty}</p>
<details class="front-panel"><summary>Installation front panel</summary><div class="panel-body">
<p class="panel-note">Opening this panel runs local, no-spend “hello world” checks. A component turns green only after it responds successfully.</p>
<button type="button" id="run-checks">Run checks again</button><div id="components" aria-live="polite"><p>Checks have not run yet.</p></div>
</div></details>
<h2>Documentation</h2><ul>{"".join(rows)}</ul>
<p>HTML documents open rendered. Markdown links expose the exact source shipped in this bundle.</p>
</main><script>
const panel = document.querySelector('.front-panel');
const target = document.querySelector('#components');
const button = document.querySelector('#run-checks');
let hasRun = false;
function esc(value) {{ const node = document.createElement('span'); node.textContent = String(value); return node.innerHTML; }}
async function runChecks() {{
  hasRun = true; button.disabled = true; button.textContent = 'Checking…';
  target.innerHTML = '<p>Checking installed components…</p>';
  try {{
    const response = await fetch('/__pack/checks', {{cache: 'no-store'}});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'checks unavailable');
    target.innerHTML = payload.checks.map(check => `<div class="component ${{esc(check.state)}}"><i class="lamp" aria-hidden="true"></i><strong>${{esc(check.label)}}</strong><small>${{esc(check.detail)}}</small></div>`).join('');
  }} catch (error) {{
    target.innerHTML = `<div class="component fail"><i class="lamp"></i><strong>Front panel</strong><small>${{esc(error)}}. Run this page with ./launch-docs.py.</small></div>`;
  }} finally {{ button.disabled = false; button.textContent = 'Run checks again'; }}
}}
panel.addEventListener('toggle', () => {{ if (panel.open && !hasRun) runChecks(); }});
button.addEventListener('click', runChecks);
</script></body></html>"""


def build_bundle(
    root: Path,
    spec: PackSpec,
    output_dir: Path,
    *,
    max_file_bytes: int = 20 * 1024 * 1024,
    allow_large_files: bool = False,
) -> BuildResult:
    root = root.resolve()
    files = source_files(root, spec.exclude)
    installer = root / "deploy" / "install.py"
    docs_launcher = root / "deploy" / "docs.py"
    component_checks = root / "deploy" / "checks.py"
    service_registry = root / "deploy" / "service_registry.py"
    if Path("deploy/install.py") not in files or not installer.is_file():
        raise PackError("deploy/install.py must be present and included in the source set")
    if Path("deploy/docs.py") not in files or not docs_launcher.is_file():
        raise PackError("deploy/docs.py must be present and included in the source set")
    if Path("deploy/checks.py") not in files or not component_checks.is_file():
        raise PackError("deploy/checks.py must be present and included in the source set")
    if spec.registry is not None and (
        Path("deploy/service_registry.py") not in files or not service_registry.is_file()
    ):
        raise PackError("deploy/service_registry.py must be present when [registry] is configured")
    large = _large_files(root, files, max_file_bytes)
    if large and not allow_large_files:
        joined = ", ".join(path.as_posix() for path in large)
        raise PackError(f"files exceed the size limit: {joined}; rerun with --allow-large-files")

    version = _project_version(root)
    short_commit = _git(root, "rev-parse", "--short=12", "HEAD")
    dirty = bool(_git(root, "status", "--porcelain=v1"))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dirty_label = "-dirty" if dirty else ""
    bundle_id = f"{version}-{short_commit}{dirty_label}-{timestamp}"
    bundle_root = f"{spec.name}-{bundle_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{bundle_root}.tar.gz"

    with tempfile.TemporaryDirectory(prefix=f"{spec.name}-pack-") as temporary:
        staging = Path(temporary) / bundle_root
        source_dir = staging / "source"
        source_dir.mkdir(parents=True)
        byte_count = _copy_source(root, source_dir, files)
        shutil.copy2(installer, staging / "install.py")
        (staging / "install.py").chmod(0o755)
        shutil.copy2(docs_launcher, staging / "launch-docs.py")
        (staging / "launch-docs.py").chmod(0o755)
        bundled_deploy = staging / "deploy"
        bundled_deploy.mkdir()
        (bundled_deploy / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(component_checks, bundled_deploy / "checks.py")
        if spec.registry is not None:
            shutil.copy2(service_registry, bundled_deploy / "service_registry.py")
        manifest = _manifest(root, spec, bundle_id)
        (staging / "pack-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging / "docs-index.html").write_text(
            _docs_index(manifest, files),
            encoding="utf-8",
        )
        with tarfile.open(archive, "w:gz", compresslevel=9) as bundle:
            bundle.add(staging, arcname=bundle_root, recursive=True)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return BuildResult(
        archive=archive,
        checksum=checksum,
        bundle_root=bundle_root,
        file_count=len(files),
        byte_count=byte_count,
    )


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        prog="pack.py",
        description="Build a secret-free, self-installing Ubuntu deployment bundle.",
    )
    parser.add_argument("--config", type=Path, default=root / "deploy/pack.toml")
    parser.add_argument("--output-dir", type=Path, default=root / "dist")
    parser.add_argument("--max-file-mb", type=int, default=20)
    parser.add_argument("--allow-large-files", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_bundle(
            root,
            load_spec(args.config),
            args.output_dir.expanduser(),
            max_file_bytes=args.max_file_mb * 1024 * 1024,
            allow_large_files=args.allow_large_files,
        )
    except (OSError, PackError, subprocess.SubprocessError, tomllib.TOMLDecodeError) as exc:
        parser.exit(1, f"pack: {exc}\n")

    size_mb = result.archive.stat().st_size / (1024 * 1024)
    print(f"Built {result.archive} ({size_mb:.2f} MB, {result.file_count} source files)")
    print(f"Checksum: {result.checksum}")
    print("\nTransfer and install:")
    print(f"  scp {result.archive} {result.checksum} lobster:")
    print(f"  ssh lobster 'sha256sum -c {result.checksum.name}'")
    print(f"  ssh lobster 'tar -xzf {result.archive.name} && ./{result.bundle_root}/install.py'")
    print(f"  # Human docs after unpacking: ./{result.bundle_root}/launch-docs.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
