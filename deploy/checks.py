"""Standard-library component checks shared by a tool-pack installer and front panel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

CheckState = Literal["pass", "fail", "skip"]


def route_paths(route: Mapping[str, Any]) -> list[str]:
    """Every path a route fronts, newest manifest shape or the older one.

    Routes carry a ``paths`` list so one endpoint can front several paths.
    Manifests built before that carry a single ``path``; bundles outlive the
    builder that made them, so both are read.
    """
    declared = route.get("paths")
    if isinstance(declared, list):
        return [str(item) for item in declared]
    single = route.get("path")
    return [str(single)] if single is not None else []


@dataclass(frozen=True)
class CheckResult:
    key: str
    label: str
    state: CheckState
    detail: str
    required: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _expanded(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser().resolve()


def _read_env_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            if name.strip() and value.strip():
                names.add(name.strip())
    return names


def _command_check(entrypoint: str, timeout_seconds: float) -> CheckResult:
    executable = Path.home() / ".local" / "bin" / entrypoint
    if not executable.exists():
        found = shutil.which(entrypoint)
        executable = Path(found) if found else executable
    if not executable.exists():
        return CheckResult("command", "Installed command", "fail", f"missing {entrypoint}")
    try:
        completed = subprocess.run(
            [str(executable), "--help"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("command", "Installed command", "fail", str(exc))
    if completed.returncode != 0:
        return CheckResult(
            "command",
            "Installed command",
            "fail",
            f"{entrypoint} --help exited {completed.returncode}",
        )
    return CheckResult("command", "Installed command", "pass", f"{entrypoint} --help")


def _service_check(unit: str, timeout_seconds: float) -> CheckResult:
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return CheckResult(
            "service",
            "User service",
            "skip",
            "systemd user services are unavailable on this host",
        )
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            completed = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CheckResult("service", "User service", "fail", str(exc))
        state = completed.stdout.strip() or completed.stderr.strip() or "unknown"
        if completed.returncode == 0 and state == "active":
            return CheckResult("service", "User service", "pass", f"{unit}: {state}")
        if state not in {"activating", "reloading"} or time.monotonic() >= deadline:
            return CheckResult("service", "User service", "fail", f"{unit}: {state}")
        time.sleep(0.2)


def _http_check(url: str, expected_service: str, timeout_seconds: float) -> CheckResult:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            body = response.read(64 * 1024)
            if not 200 <= response.status < 300:
                raise OSError(f"HTTP {response.status}")
            payload = json.loads(body)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return CheckResult("health", "HTTP health", "fail", f"{url}: {exc}")
    if not isinstance(payload, dict) or payload.get("service") != expected_service:
        actual = payload.get("service") if isinstance(payload, dict) else None
        return CheckResult(
            "health",
            "HTTP health",
            "fail",
            f"{url}: expected {expected_service!r}, received {actual!r}",
        )
    version = payload.get("version") if isinstance(payload, dict) else None
    detail = f"{url} · version {version}" if version else url
    return CheckResult("health", "HTTP health", "pass", detail)


def tailscale_route_state(
    config: Mapping[str, Any], timeout_seconds: float = 5.0
) -> tuple[str, str]:
    """Return route state and a token-free diagnostic for a Tailscale path mount."""
    mode = str(config.get("mode", ""))
    path = "/" + str(config.get("path", "")).strip("/")
    target = str(config.get("target", ""))
    port = config.get("port", 443)
    if mode not in {"serve", "funnel"} or path == "/" or not target or not isinstance(port, int):
        return "unavailable", "invalid Tailscale route configuration"
    if shutil.which("tailscale") is None:
        return "unavailable", "tailscale command is not installed"
    try:
        result = subprocess.run(
            ["tailscale", mode, "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return "unavailable", f"could not inspect Tailscale: {exc}"
    if result.returncode != 0 or not isinstance(payload, dict):
        return "unavailable", "tailscale route status was unavailable"

    web = payload.get("Web")
    if not isinstance(web, dict):
        return "missing", f"no HTTPS route maps {path} to {target}"
    conflict: str | None = None
    for authority, raw_site in web.items():
        if not str(authority).endswith(f":{port}") or not isinstance(raw_site, dict):
            continue
        handlers = raw_site.get("Handlers")
        if not isinstance(handlers, dict) or path not in handlers:
            continue
        handler = handlers[path]
        actual = handler.get("Proxy") if isinstance(handler, dict) else None
        if actual != target:
            conflict = f"{authority}{path} already maps to {actual!r}, not {target!r}"
            continue
        if mode == "funnel":
            allowed = payload.get("AllowFunnel")
            if not isinstance(allowed, dict) or allowed.get(authority) is not True:
                return "missing", f"{authority}{path} is served but Funnel is not enabled"
        return "pass", f"https://{authority}{path} → {target}"
    if conflict:
        return "conflict", conflict
    return "missing", f"no HTTPS route maps {path} to {target}"


def service_registry_state(config: Mapping[str, Any]) -> tuple[bool, str]:
    helper = Path(str(config.get("helper_path", "")))
    data_path = Path(str(config.get("data_path", "")))
    entries = config.get("entries")
    if not helper.is_file():
        return False, f"registry helper is missing: {helper}"
    if not data_path.is_file():
        return False, f"registry data is missing: {data_path}"
    if not isinstance(entries, list):
        return False, "registry manifest entries are invalid"
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"cannot read registry data: {exc}"
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        return False, "registry data has an unsupported format"
    services = payload.get("services")
    if not isinstance(services, dict):
        return False, "registry data has no services table"
    for expected in entries:
        if not isinstance(expected, dict):
            return False, "registry manifest entry is invalid"
        service_id = str(expected.get("service_id", ""))
        actual = services.get(service_id)
        if not isinstance(actual, dict):
            return False, f"{service_id} is not reserved"
        endpoint = actual.get("endpoint")
        expected_endpoint = {
            "protocol": expected.get("protocol"),
            "address": expected.get("address"),
            "port": expected.get("port"),
        }
        if not isinstance(endpoint, dict) or any(
            endpoint.get(key) != value for key, value in expected_endpoint.items()
        ):
            return False, f"{service_id} endpoint differs from the pack declaration"
        expected_route = expected.get("route")
        if expected_route is not None:
            declared = route_paths(expected_route)
            reserved = actual.get("routes")
            actual_routes = (
                [item for item in reserved if isinstance(item, dict)]
                if isinstance(reserved, list)
                else ([actual["route"]] if isinstance(actual.get("route"), dict) else [])
            )
            if [str(item.get("path")) for item in actual_routes] != declared:
                return False, f"{service_id} external route differs from the pack declaration"
            for item in actual_routes:
                if any(
                    item.get(key) != expected_route.get(key)
                    for key in ("mode", "host", "https_port", "target")
                ):
                    return False, f"{service_id} external route differs from the pack declaration"
    return True, f"{data_path} · {len(entries)} declared services reserved"


def run_component_checks(
    manifest: Mapping[str, Any],
    bundle_root: Path,
    *,
    install_root: Path | None = None,
    include_services: bool = True,
    timeout_seconds: float = 3.0,
) -> list[CheckResult]:
    """Run local, no-spend checks without returning secret values."""
    install = manifest.get("install")
    tool = manifest.get("tool")
    services = manifest.get("services")
    if (
        not isinstance(install, dict)
        or not isinstance(tool, dict)
        or not isinstance(services, list)
    ):
        return [CheckResult("manifest", "Pack manifest", "fail", "invalid manifest shape")]

    install_root = install_root or _expanded(str(install.get("default_root", "")))
    current = install_root / "current"
    release_ok = current.is_symlink() and current.resolve().is_dir()
    checks = [
        CheckResult(
            "release",
            "Current release",
            "pass" if release_ok else "fail",
            str(current.resolve()) if release_ok else f"missing managed link at {current}",
        )
    ]

    entrypoints = [
        str(service.get("entrypoint", ""))
        for service in services
        if isinstance(service, dict) and service.get("entrypoint")
    ]
    if entrypoints:
        checks.append(_command_check(entrypoints[0], timeout_seconds))

    environment_file = _expanded(str(install.get("environment_file", "")))
    repo_environment = str(install.get("repo_environment", ""))
    raw_environment = install.get("environment", {})
    environment_names = (
        [str(name) for name in raw_environment] if isinstance(raw_environment, dict) else []
    )
    expected_environment = [name for name in [repo_environment, *environment_names] if name]
    configured_environment = _read_env_names(environment_file)
    missing_environment = [
        name for name in expected_environment if name not in configured_environment
    ]
    checks.append(
        CheckResult(
            "host_configuration",
            "Host configuration",
            "pass" if expected_environment and not missing_environment else "fail",
            (
                str(environment_file)
                if expected_environment and not missing_environment
                else "missing " + ", ".join(missing_environment or ["manifest settings"])
            ),
        )
    )
    pack_manager = manifest.get("pack_manager")
    if isinstance(pack_manager, dict):
        manager_path = _expanded(str(pack_manager.get("install_path", "")))
        manager_ready = (
            manager_path.is_file()
            and "tool-pack-manager-format: 1"
            in manager_path.read_text(encoding="utf-8", errors="replace")[:512]
        )
        checks.append(
            CheckResult(
                "pack_manager",
                "Pack manager",
                "pass" if manager_ready else "fail",
                str(manager_path)
                if manager_ready
                else f"missing managed command at {manager_path}",
            )
        )

    secrets_file = _expanded(str(install.get("secrets_file", "")))
    required = install.get("required_secrets", [])
    required_names = [str(name) for name in required] if isinstance(required, list) else []
    configured = _read_env_names(secrets_file)
    missing = [name for name in required_names if name not in configured]
    checks.append(
        CheckResult(
            "configuration",
            "Research credentials",
            "pass" if not missing else "fail",
            str(secrets_file) if not missing else "missing " + ", ".join(missing),
            required=False,
        )
    )

    if include_services:
        for service in services:
            if not isinstance(service, dict):
                continue
            checks.append(_service_check(str(service.get("unit", "")), timeout_seconds))
            checks.append(
                _http_check(
                    str(service.get("health_url", "")),
                    str(service.get("health_service", "")),
                    timeout_seconds,
                )
            )
    else:
        checks.extend(
            [
                CheckResult("service", "User service", "skip", "service installation skipped"),
                CheckResult("health", "HTTP health", "skip", "service installation skipped"),
            ]
        )

    capability = manifest.get("capability")
    if isinstance(capability, dict):
        token_file = _expanded(str(capability.get("token_file", "")))
        token_ready = token_file.is_file() and bool(token_file.read_text(encoding="utf-8").strip())
        checks.append(
            CheckResult(
                "capability",
                "MCP capability",
                "pass" if token_ready else ("fail" if include_services else "skip"),
                str(token_file) if token_ready else "capability token has not been created",
            )
        )

    tailscale = manifest.get("tailscale")
    if isinstance(tailscale, dict):
        required_tunnel = bool(tailscale.get("required", False))
        if include_services:
            route_state, detail = tailscale_route_state(tailscale, timeout_seconds)
            checks.append(
                CheckResult(
                    "tailscale",
                    "Tailscale front door",
                    "pass" if route_state == "pass" else "fail",
                    detail,
                    required=required_tunnel,
                )
            )
        else:
            checks.append(
                CheckResult(
                    "tailscale",
                    "Tailscale front door",
                    "skip",
                    "service installation skipped",
                    required=required_tunnel,
                )
            )

    registry = manifest.get("registry")
    if isinstance(registry, dict):
        required_registry = bool(registry.get("required", False))
        if include_services:
            ready, detail = service_registry_state(registry)
            checks.append(
                CheckResult(
                    "registry",
                    "Host service registry",
                    "pass" if ready else "fail",
                    detail,
                    required=required_registry,
                )
            )
        else:
            checks.append(
                CheckResult(
                    "registry",
                    "Host service registry",
                    "skip",
                    "service installation skipped",
                    required=required_registry,
                )
            )

    docs_ready = (bundle_root / "docs-index.html").is_file() and (bundle_root / "source").is_dir()
    checks.append(
        CheckResult(
            "documentation",
            "Documentation",
            "pass" if docs_ready else "fail",
            str(bundle_root / "docs-index.html")
            if docs_ready
            else "documentation payload incomplete",
        )
    )
    return checks


def required_checks_pass(checks: Sequence[CheckResult]) -> bool:
    return all(check.state != "fail" for check in checks if check.required)


def print_component_panel(checks: Sequence[CheckResult]) -> None:
    use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    colors = {"pass": "\033[32m", "fail": "\033[31m", "skip": "\033[33m"}
    reset = "\033[0m" if use_color else ""
    print("\nComponent check")
    for check in checks:
        marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[check.state]
        color = colors[check.state] if use_color else ""
        print(f"  {color}{marker:4}{reset}  {check.label}: {check.detail}")
