#!/usr/bin/env python3
# common-services-registry-format: 1
"""Host-wide service endpoint registry for small shared Ubuntu machines.

The registry contains no credentials. Mutations are serialized with ``flock``
and committed with atomic replacement. The production file is root-owned and
world-readable so operators can inspect it without granting write access.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
DEFAULT_REGISTRY = Path("/var/lib/common-services/registry.json")
DEFAULT_STATIC_RANGE = (8700, 8799)
DEFAULT_DYNAMIC_RANGE = (8800, 8999)
_SERVICE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_WILDCARD = {"0.0.0.0", "::"}


class RegistryError(RuntimeError):
    """A reservation or registry operation is invalid or unsafe."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def parse_range(value: str) -> tuple[int, int]:
    try:
        start_text, end_text = value.split("-", 1)
        start, end = int(start_text), int(end_text)
    except (ValueError, TypeError) as exc:
        raise RegistryError(f"invalid port range {value!r}; expected START-END") from exc
    if not 1024 <= start <= end <= 49151:
        raise RegistryError(f"invalid port range {value!r}; use ports 1024-49151")
    return start, end


def validate_ranges(static_range: tuple[int, int], dynamic_range: tuple[int, int]) -> None:
    if max(static_range[0], dynamic_range[0]) <= min(static_range[1], dynamic_range[1]):
        raise RegistryError("static and dynamic port ranges must not overlap")


def normalize_path(value: str) -> str:
    cleaned = value.strip().strip("/")
    return f"/{cleaned}" if cleaned else "/"


def _valid_port(value: int) -> int:
    if not 1024 <= value <= 49151:
        raise RegistryError(f"port {value} is outside the supported user-port range 1024-49151")
    return value


def _addresses_conflict(left: str, right: str) -> bool:
    canonical = {"localhost": "127.0.0.1"}
    left = canonical.get(left, left)
    right = canonical.get(right, right)
    return left == right or left in _WILDCARD or right in _WILDCARD


def _port_available(address: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    target = "::1" if address == "localhost" and family == socket.AF_INET6 else address
    if address == "localhost":
        target = "127.0.0.1"
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((target, port))
        except OSError:
            return False
    return True


def _empty_registry(
    static_range: tuple[int, int], dynamic_range: tuple[int, int]
) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "updated_at": _now(),
        "port_ranges": {
            "static": list(static_range),
            "dynamic": list(dynamic_range),
        },
        "services": {},
    }


def _validate_registry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        raise RegistryError("unsupported or invalid service registry")
    if not isinstance(value.get("port_ranges"), dict) or not isinstance(
        value.get("services"), dict
    ):
        raise RegistryError("service registry is missing port_ranges or services")
    ranges = value["port_ranges"]
    try:
        static_range = tuple(ranges["static"])
        dynamic_range = tuple(ranges["dynamic"])
        if len(static_range) != 2 or len(dynamic_range) != 2:
            raise ValueError
        for start, end in (static_range, dynamic_range):
            if not all(
                isinstance(item, int) and not isinstance(item, bool) for item in (start, end)
            ):
                raise ValueError
            if not 1024 <= start <= end <= 49151:
                raise ValueError
        validate_ranges(static_range, dynamic_range)
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryError("service registry has invalid port ranges") from exc
    for service_id, entry in value["services"].items():
        if not isinstance(service_id, str) or not _SERVICE_ID.fullmatch(service_id):
            raise RegistryError(f"service registry has invalid service id {service_id!r}")
        if not isinstance(entry, dict) or entry.get("service_id") != service_id:
            raise RegistryError(f"service registry entry {service_id!r} has invalid identity")
        endpoint = entry.get("endpoint")
        if not isinstance(endpoint, dict):
            raise RegistryError(f"service registry entry {service_id!r} has no endpoint")
        address = endpoint.get("address")
        port = endpoint.get("port")
        allocation = endpoint.get("allocation")
        if (
            endpoint.get("protocol") != "tcp"
            or address not in _LOOPBACK
            or not isinstance(port, int)
            or isinstance(port, bool)
            or allocation not in {"static", "dynamic"}
        ):
            raise RegistryError(f"service registry entry {service_id!r} has invalid endpoint")
        band = static_range if allocation == "static" else dynamic_range
        if not band[0] <= port <= band[1]:
            raise RegistryError(
                f"service registry entry {service_id!r} is outside its {allocation} range"
            )
    return value


class RegistryStore:
    def __init__(
        self,
        path: Path = DEFAULT_REGISTRY,
        *,
        static_range: tuple[int, int] = DEFAULT_STATIC_RANGE,
        dynamic_range: tuple[int, int] = DEFAULT_DYNAMIC_RANGE,
    ) -> None:
        validate_ranges(static_range, dynamic_range)
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.static_range = static_range
        self.dynamic_range = dynamic_range

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_registry(self.static_range, self.dynamic_range)
        try:
            return _validate_registry(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"cannot read {self.path}: {exc}") from exc

    def _ensure_ranges(self, data: Mapping[str, Any]) -> None:
        expected = {
            "static": list(self.static_range),
            "dynamic": list(self.dynamic_range),
        }
        if data.get("port_ranges") != expected:
            raise RegistryError(
                f"registry port ranges are {data.get('port_ranges')!r}, not requested {expected!r}"
            )

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        data["updated_at"] = _now()
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o644)
            os.replace(temporary_name, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @contextmanager
    def mutate(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            data = self.load()
            self._ensure_ranges(data)
            yield data
            _validate_registry(data)
            self._write(data)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _service(value: str) -> str:
    if not _SERVICE_ID.fullmatch(value):
        raise RegistryError(f"invalid service id {value!r}")
    return value


def _endpoint_conflict(
    services: Mapping[str, Any],
    service_id: str,
    protocol: str,
    address: str,
    port: int,
) -> tuple[str, Mapping[str, Any]] | None:
    for owner_id, raw in services.items():
        if owner_id == service_id or not isinstance(raw, dict):
            continue
        endpoint = raw.get("endpoint")
        if not isinstance(endpoint, dict):
            continue
        if (
            endpoint.get("protocol") == protocol
            and endpoint.get("port") == port
            and _addresses_conflict(str(endpoint.get("address")), address)
        ):
            return str(owner_id), endpoint
    return None


def entry_routes(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every route an entry holds.

    Entries carry a ``routes`` list so one endpoint can front several paths.
    ``route`` is kept alongside it, holding the first, so readers written
    against the single-route shape keep working.
    """
    routes = entry.get("routes")
    if isinstance(routes, list):
        return [item for item in routes if isinstance(item, dict)]
    route = entry.get("route")
    return [route] if isinstance(route, dict) else []


def _route_conflict(
    services: Mapping[str, Any],
    service_id: str,
    host: str,
    https_port: int,
    path: str,
) -> str | None:
    for owner_id, raw in services.items():
        if owner_id == service_id or not isinstance(raw, dict):
            continue
        for route in entry_routes(raw):
            if (
                route.get("host") == host
                and route.get("https_port") == https_port
                and route.get("path") == path
            ):
                return str(owner_id)
    return None


def reserve_service(
    store: RegistryStore,
    *,
    service_id: str,
    display_name: str,
    owner: str,
    address: str,
    protocol: str = "tcp",
    port: int | None = None,
    unit: str | None = None,
    health_url: str | None = None,
    health_service: str | None = None,
    source: str | None = None,
    adopt_listener: bool = False,
) -> dict[str, Any]:
    service_id = _service(service_id)
    if protocol != "tcp":
        raise RegistryError("registry v1 supports TCP endpoints only")
    if address not in _LOOPBACK:
        raise RegistryError("registry v1 supports loopback addresses only")
    if bool(health_url) != bool(health_service):
        raise RegistryError("health_url and health_service must be supplied together")

    with store.mutate() as data:
        services = data["services"]
        assert isinstance(services, dict)
        existing = services.get(service_id)
        if port is None:
            existing_endpoint = existing.get("endpoint") if isinstance(existing, dict) else None
            if (
                isinstance(existing_endpoint, dict)
                and existing_endpoint.get("allocation") == "dynamic"
                and existing_endpoint.get("protocol") == protocol
                and existing_endpoint.get("address") == address
                and isinstance(existing_endpoint.get("port"), int)
            ):
                port = int(existing_endpoint["port"])
            else:
                start, end = store.dynamic_range
                selected = next(
                    (
                        candidate
                        for candidate in range(start, end + 1)
                        if _endpoint_conflict(services, service_id, protocol, address, candidate)
                        is None
                        and _port_available(address, candidate)
                    ),
                    None,
                )
                if selected is None:
                    raise RegistryError(f"no free port remains in dynamic range {start}-{end}")
                port = selected
            allocation = "dynamic"
        else:
            port = _valid_port(port)
            if not store.static_range[0] <= port <= store.static_range[1]:
                raise RegistryError(
                    f"static port {port} is outside range "
                    f"{store.static_range[0]}-{store.static_range[1]}"
                )
            allocation = "static"
        conflict = _endpoint_conflict(services, service_id, protocol, address, port)
        if conflict:
            owner_id, endpoint = conflict
            raise RegistryError(
                f"{protocol} {address}:{port} is reserved by {owner_id} ({endpoint!r})"
            )

        existing_endpoint = existing.get("endpoint") if isinstance(existing, dict) else None
        same_endpoint = isinstance(existing_endpoint, dict) and all(
            existing_endpoint.get(key) == value
            for key, value in {"protocol": protocol, "address": address, "port": port}.items()
        )
        if not same_endpoint and not adopt_listener and not _port_available(address, port):
            raise RegistryError(
                f"{protocol} {address}:{port} is already in use; pass --adopt-listener only "
                "when importing the known owner"
            )
        created_at = existing.get("created_at") if isinstance(existing, dict) else _now()
        routes = entry_routes(existing) if isinstance(existing, dict) else []
        route = routes[0] if routes else None
        entry = {
            "service_id": service_id,
            "display_name": display_name,
            "owner": owner,
            "unit": unit,
            "source": source,
            "endpoint": {
                "protocol": protocol,
                "address": address,
                "port": port,
                "allocation": allocation,
            },
            "health": ({"url": health_url, "service": health_service} if health_url else None),
            "route": route,
            "routes": routes,
            "created_at": created_at,
            "updated_at": _now(),
        }
        if isinstance(existing, dict) and existing != entry and not same_endpoint:
            raise RegistryError(
                f"{service_id} already has a different endpoint; release or migrate it explicitly"
            )
        services[service_id] = entry
        return entry


def reserve_route(
    store: RegistryStore,
    *,
    service_id: str,
    host: str,
    https_port: int,
    paths: Sequence[str],
    mode: str,
    target: str,
) -> dict[str, Any]:
    service_id = _service(service_id)
    if mode not in {"serve", "funnel"}:
        raise RegistryError("route mode must be serve or funnel")
    if not paths:
        raise RegistryError("a route needs at least one path")
    https_port = _valid_port(https_port) if https_port != 443 else 443
    normalized: list[str] = []
    for candidate in paths:
        value = normalize_path(candidate)
        if value in normalized:
            raise RegistryError(f"path is declared twice: {value}")
        normalized.append(value)
    with store.mutate() as data:
        services = data["services"]
        assert isinstance(services, dict)
        entry = services.get(service_id)
        if not isinstance(entry, dict):
            raise RegistryError(f"reserve {service_id}'s local endpoint before its route")
        for path in normalized:
            conflict = _route_conflict(services, service_id, host, https_port, path)
            if conflict:
                raise RegistryError(f"https://{host}:{https_port}{path} is reserved by {conflict}")
        routes = [
            {
                "mode": mode,
                "host": host,
                "https_port": https_port,
                "path": path,
                "target": target,
            }
            for path in normalized
        ]
        existing = entry_routes(entry)
        if existing and existing != routes:
            raise RegistryError(f"{service_id} already has different routes; migrate it explicitly")
        entry["routes"] = routes
        # Kept in step so readers written against the single-route shape,
        # including older copies of this helper, still see a route.
        entry["route"] = routes[0]
        entry["updated_at"] = _now()
        return entry


def release_service(store: RegistryStore, service_id: str) -> dict[str, Any]:
    service_id = _service(service_id)
    with store.mutate() as data:
        services = data["services"]
        assert isinstance(services, dict)
        try:
            value = services.pop(service_id)
        except KeyError as exc:
            raise RegistryError(f"{service_id} is not reserved") from exc
        assert isinstance(value, dict)
        return value


def _listener_ready(endpoint: Mapping[str, Any], timeout: float = 0.3) -> bool:
    address = str(endpoint.get("address", ""))
    if address == "localhost":
        address = "127.0.0.1"
    try:
        with socket.create_connection((address, int(endpoint["port"])), timeout=timeout):
            return True
    except (OSError, ValueError, KeyError):
        return False


def _health_ready(health: Mapping[str, Any], timeout: float = 2.0) -> tuple[bool, str]:
    url = str(health.get("url", ""))
    expected = str(health.get("service", ""))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read(64 * 1024))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return False, str(exc)
    actual = payload.get("service") if isinstance(payload, dict) else None
    return actual == expected, f"expected {expected!r}, received {actual!r}"


def _tailscale_status(
    route: Mapping[str, Any], runner: Callable[..., Any] = subprocess.run
) -> tuple[bool, str]:
    mode = str(route.get("mode", ""))
    try:
        result = runner(
            ["tailscale", mode, "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return False, str(exc)
    for authority, raw_site in payload.get("Web", {}).items():
        if not str(authority).endswith(f":{route.get('https_port')}"):
            continue
        handlers = raw_site.get("Handlers", {}) if isinstance(raw_site, dict) else {}
        handler = handlers.get(route.get("path")) if isinstance(handlers, dict) else None
        if isinstance(handler, dict) and handler.get("Proxy") == route.get("target"):
            if mode == "funnel" and payload.get("AllowFunnel", {}).get(authority) is not True:
                return False, "route exists but Funnel is disabled"
            return True, f"https://{authority}{route.get('path')}"
    return False, "declared route is not configured"


def _unit_status(
    owner: str,
    unit: str,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[bool | None, str]:
    if not unit:
        return None, "not configured"
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return None, "systemd user services are unavailable"
    try:
        uid = pwd.getpwnam(owner).pw_uid
    except KeyError:
        return False, f"Unix owner {owner!r} does not exist"
    command = ["systemctl", "--user", "is-active", unit]
    if owner != getpass.getuser():
        if os.geteuid() != 0:
            return None, "another user's systemd manager requires root reconciliation"
        if shutil.which("runuser") is None:
            return None, "cannot inspect another user's systemd manager without runuser"
        runtime = f"/run/user/{uid}"
        command = [
            "runuser",
            "-u",
            owner,
            "--",
            "env",
            f"XDG_RUNTIME_DIR={runtime}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
            *command,
        ]
    try:
        result = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    state = result.stdout.strip() or result.stderr.strip() or "unknown"
    return result.returncode == 0 and state == "active", state


def observe_service(entry: Mapping[str, Any]) -> dict[str, Any]:
    endpoint = entry.get("endpoint")
    listener = isinstance(endpoint, dict) and _listener_ready(endpoint)
    health_value = entry.get("health")
    health_ok, health_detail = (
        _health_ready(health_value) if isinstance(health_value, dict) else (None, "not configured")
    )
    routes = entry_routes(entry)
    if routes:
        observed = [_tailscale_status(route) for route in routes]
        route_ok = all(ok for ok, _ in observed)
        # Report every path, so one broken route in a set is not averaged away.
        route_detail = " · ".join(detail for _, detail in observed)
    else:
        route_ok, route_detail = None, "not configured"
    unit_ok, unit_detail = _unit_status(str(entry.get("owner", "")), str(entry.get("unit") or ""))
    required = [listener]
    if health_ok is not None:
        required.append(health_ok)
    if route_ok is not None:
        required.append(route_ok)
    if unit_ok is not None:
        required.append(unit_ok)
    if not all(required):
        state = "stale"
    elif health_ok is True:
        state = "healthy"
    elif listener or unit_ok is True:
        state = "installed"
    else:
        state = "reserved"
    return {
        "service_id": entry.get("service_id"),
        "state": state,
        "listener": listener,
        "unit": unit_ok,
        "unit_detail": unit_detail,
        "health": health_ok,
        "health_detail": health_detail,
        "route": route_ok,
        "route_detail": route_detail,
    }


def _managed_ports(store: RegistryStore) -> Iterator[int]:
    seen: set[int] = set()
    for start, end in (store.static_range, store.dynamic_range):
        for port in range(start, end + 1):
            if port not in seen:
                seen.add(port)
                yield port


def unknown_listeners(store: RegistryStore, services: Mapping[str, Any]) -> list[dict[str, Any]]:
    declared = {
        int(endpoint["port"])
        for raw in services.values()
        if isinstance(raw, dict)
        and isinstance((endpoint := raw.get("endpoint")), dict)
        and _addresses_conflict(str(endpoint.get("address")), "127.0.0.1")
        and isinstance(endpoint.get("port"), int)
    }
    return [
        {"protocol": "tcp", "address": "127.0.0.1", "port": port}
        for port in _managed_ports(store)
        if port not in declared and not _port_available("127.0.0.1", port)
    ]


def _entry_line(entry: Mapping[str, Any], observed: Mapping[str, Any] | None = None) -> str:
    endpoint = entry.get("endpoint", {})
    route = entry.get("route")
    route_text = "—"
    if isinstance(route, dict):
        port_text = "" if route.get("https_port") == 443 else f":{route.get('https_port')}"
        route_text = f"https://{route.get('host')}{port_text}{route.get('path')}"
    state = observed.get("state") if observed else "reserved"
    listener = "yes" if observed and observed.get("listener") else "no"
    health = "—"
    if observed and observed.get("health") is not None:
        health = "pass" if observed.get("health") else "fail"
    return (
        f"{entry.get('service_id')}\t{state}\t{endpoint.get('protocol')}://"
        f"{endpoint.get('address')}:{endpoint.get('port')}\t{listener}\t{health}\t"
        f"{route_text}\t{entry.get('owner')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="service-registry")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--static-range", default=f"{DEFAULT_STATIC_RANGE[0]}-{DEFAULT_STATIC_RANGE[1]}"
    )
    parser.add_argument(
        "--dynamic-range", default=f"{DEFAULT_DYNAMIC_RANGE[0]}-{DEFAULT_DYNAMIC_RANGE[1]}"
    )
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    reserve = commands.add_parser("reserve")
    reserve.add_argument("service_id")
    reserve.add_argument("--name", required=True)
    reserve.add_argument("--owner", default=getpass.getuser())
    reserve.add_argument("--address", default="127.0.0.1")
    reserve.add_argument("--protocol", default="tcp")
    port = reserve.add_mutually_exclusive_group(required=True)
    port.add_argument("--port", type=int)
    port.add_argument("--allocate", action="store_true")
    reserve.add_argument("--unit")
    reserve.add_argument("--health-url")
    reserve.add_argument("--health-service")
    reserve.add_argument("--source")
    reserve.add_argument("--adopt-listener", action="store_true")

    route = commands.add_parser("reserve-route")
    route.add_argument("service_id")
    route.add_argument("--host", required=True)
    route.add_argument("--https-port", type=int, default=443)
    route.add_argument("--path", action="append", dest="paths", required=True)
    route.add_argument("--mode", choices=["serve", "funnel"], required=True)
    route.add_argument("--target", required=True)

    release = commands.add_parser("release")
    release.add_argument("service_id")
    release.add_argument("--yes", action="store_true")

    commands.add_parser("list")
    check = commands.add_parser("check")
    check.add_argument("service_id", nargs="?")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("service_id", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        store = RegistryStore(
            args.registry,
            static_range=parse_range(args.static_range),
            dynamic_range=parse_range(args.dynamic_range),
        )
        result: Any
        if args.command == "reserve":
            result = reserve_service(
                store,
                service_id=args.service_id,
                display_name=args.name,
                owner=args.owner,
                address=args.address,
                protocol=args.protocol,
                port=args.port,
                unit=args.unit,
                health_url=args.health_url,
                health_service=args.health_service,
                source=args.source,
                adopt_listener=args.adopt_listener,
            )
        elif args.command == "reserve-route":
            result = reserve_route(
                store,
                service_id=args.service_id,
                host=args.host,
                https_port=args.https_port,
                paths=args.paths,
                mode=args.mode,
                target=args.target,
            )
        elif args.command == "release":
            if not args.yes:
                raise RegistryError("release requires --yes; reservations are never implicit")
            result = release_service(store, args.service_id)
        else:
            data = store.load()
            services = data["services"]
            selected = [
                entry
                for service_id, entry in sorted(services.items())
                if args.command == "list"
                or args.service_id is None
                or args.service_id == service_id
            ]
            if args.command != "list" and args.service_id is not None and not selected:
                raise RegistryError(f"{args.service_id} is not reserved")
            observations = [observe_service(entry) for entry in selected]
            if args.command == "list":
                result = [
                    {"declared": entry, "observed": observed}
                    for entry, observed in zip(selected, observations, strict=True)
                ]
            elif args.command == "reconcile":
                result = {
                    "services": observations,
                    "unknown_listeners": unknown_listeners(store, services),
                }
            else:
                result = observations

        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "list":
            print("SERVICE\tSTATE\tDECLARED ENDPOINT\tLISTENER\tHEALTH\tEXTERNAL ROUTE\tOWNER")
            for item in result:
                print(_entry_line(item["declared"], item["observed"]))
        elif args.command in {"check", "reconcile"}:
            observations = result["services"] if args.command == "reconcile" else result
            by_id = {item["service_id"]: item for item in observations}
            data = store.load()
            print("SERVICE\tSTATE\tDECLARED ENDPOINT\tLISTENER\tHEALTH\tEXTERNAL ROUTE\tOWNER")
            for service_id, entry in sorted(data["services"].items()):
                if service_id in by_id:
                    print(_entry_line(entry, by_id[service_id]))
            unknown = result.get("unknown_listeners", []) if args.command == "reconcile" else []
            for listener in unknown:
                print(
                    f"UNKNOWN\tstale\ttcp://{listener['address']}:{listener['port']}\tyes\t—\t—\t—"
                )
            return (
                0 if all(item["state"] != "stale" for item in observations) and not unknown else 1
            )
        elif args.command == "release":
            print(_entry_line(result, {"state": "released"}))
        else:
            print(_entry_line(result))
        return 0
    except (OSError, RegistryError) as exc:
        print(f"service-registry: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
