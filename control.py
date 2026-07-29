"""Local process lifecycle and upgrade command for Alexandria MCP servers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from alexandria.version import service_version

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8797
_SERVICE_UNIT = "alexandria-mcp.service"
_PROCESS_GRACE_SECONDS = 5.0
_HEALTH_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    uid: int
    tty: str
    command: str

    @property
    def is_http(self) -> bool:
        return "--http" in self.command.split()


class Runner(Protocol):
    def __call__(self, command: list[str], *, cwd: Path | None = None) -> None: ...


def _default_repo(env: dict[str, str] | os._Environ[str] | None = None) -> Path:
    environment = os.environ if env is None else env
    configured = environment.get("ALEXANDRIA_REPO", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Documents/dev/alexandria").resolve()


def _default_log(env: dict[str, str] | os._Environ[str] | None = None) -> Path:
    environment = os.environ if env is None else env
    configured = environment.get("ALEXANDRIA_LOG", "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Logs/alexandria-mcp.log"
    return Path.home() / ".local/state/alexandria-mcp.log"


def _run_checked(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _parse_processes(output: str) -> list[ProcessInfo]:
    processes: list[ProcessInfo] = []
    for raw_line in output.splitlines():
        fields = raw_line.strip().split(maxsplit=3)
        if len(fields) != 4:
            continue
        try:
            pid, uid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        processes.append(ProcessInfo(pid=pid, uid=uid, tty=fields[2], command=fields[3]))
    return processes


def _all_processes() -> list[ProcessInfo]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,uid=,tty=,command="],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return _parse_processes(result.stdout)


def _is_alexandria_mcp(command: str) -> bool:
    return any(
        token == "alexandria-mcp" or token.endswith("/alexandria-mcp") for token in command.split()
    )


def mcp_processes(processes: list[ProcessInfo] | None = None) -> list[ProcessInfo]:
    """Return only this account's Alexandria MCP processes."""
    candidates = _all_processes() if processes is None else processes
    my_uid = os.getuid()
    ignored = {os.getpid(), os.getppid()}
    return [
        process
        for process in candidates
        if process.uid == my_uid
        and process.pid not in ignored
        and _is_alexandria_mcp(process.command)
    ]


def _systemd_command(action: str) -> list[str]:
    return ["systemctl", "--user", action, _SERVICE_UNIT]


def systemd_unit_installed() -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        _systemd_command("cat"),
        capture_output=True,
        check=False,
        timeout=5,
    )
    return result.returncode == 0


def _health(host: str, port: int) -> tuple[bool, str]:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, ValueError):
        return False, f"unhealthy — {url} did not return Alexandria health"
    if not isinstance(payload, dict) or payload.get("service") != "alexandria":
        return False, f"unhealthy — {url} did not identify itself as Alexandria"
    return (
        True,
        f"healthy — v{payload.get('version', '?')} since {payload.get('started_at', '?')} ({url})",
    )


def _wait_for_health(host: str, port: int) -> tuple[bool, str]:
    deadline = time.monotonic() + _HEALTH_WAIT_SECONDS
    result = _health(host, port)
    while not result[0] and time.monotonic() < deadline:
        time.sleep(0.25)
        result = _health(host, port)
    return result


def _stop_process(process: ProcessInfo) -> str:
    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"pid {process.pid} already stopped"
    deadline = time.monotonic() + _PROCESS_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(process.pid, 0)
        except ProcessLookupError:
            return f"stopped pid {process.pid}"
        time.sleep(0.1)
    try:
        os.kill(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return f"stopped pid {process.pid}"
    return f"stopped pid {process.pid} (forced after {_PROCESS_GRACE_SECONDS:g}s)"


def stop_all(
    *,
    use_systemd: bool,
    force: bool = False,
    stream: TextIO = sys.stdout,
    input_fn: Callable[[str], str] = input,
) -> int:
    processes = mcp_processes()
    stdio = [process for process in processes if not process.is_http]
    if stdio and not force:
        print("This will stop client-owned stdio servers:", file=stream)
        for process in stdio:
            print(f"  pid {process.pid} — tty {process.tty}", file=stream)
        answer = str(input_fn("Those clients lose Alexandria until restarted. Continue? [y/N] "))
        if answer.strip().lower() != "y":
            print("aborted; no process was stopped", file=stream)
            return 0
    if use_systemd:
        subprocess.run(_systemd_command("stop"), check=True)
    for process in processes:
        owner = "HTTP" if process.is_http else "client-owned stdio"
        print(f"  {_stop_process(process)} — {owner}", file=stream)
    if not processes and not use_systemd:
        print("  no Alexandria MCP processes were running", file=stream)
    if stdio:
        print(
            "  restart Claude Desktop / CLI sessions to reconnect their stdio servers",
            file=stream,
        )
    return len(processes)


def _server_executable() -> str:
    sibling = Path(sys.executable).with_name("alexandria-mcp")
    if sibling.is_file():
        return str(sibling)
    executable = shutil.which("alexandria-mcp")
    if executable is None:
        raise RuntimeError("alexandria-mcp is not installed; run: uv tool install .")
    return executable


def start_server(
    repo: Path,
    host: str,
    port: int,
    *,
    use_systemd: bool,
    stream: TextIO = sys.stdout,
) -> bool:
    if use_systemd:
        subprocess.run(_systemd_command("start"), check=True)
    elif any(process.is_http for process in mcp_processes()):
        print("  HTTP server is already running", file=stream)
    else:
        log_path = _default_log()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["ALEXANDRIA_REPO"] = str(repo)
        with log_path.open("ab") as log:
            subprocess.Popen(
                [_server_executable(), "--http", "--host", host, "--port", str(port)],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print(f"  log: {log_path}", file=stream)
    healthy, detail = _wait_for_health(host, port)
    print(f"  {detail}", file=stream)
    return healthy


def upgrade(repo: Path, *, runner: Runner = _run_checked, stream: TextIO = sys.stdout) -> None:
    if not (repo / ".git").exists():
        raise RuntimeError(f"Alexandria checkout not found at {repo}; set ALEXANDRIA_REPO.")
    print(f"→ refreshing {repo} from GitHub…", file=stream)
    runner(["git", "pull", "--ff-only"], cwd=repo)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is not installed or is not on PATH.")
    print("→ reinstalling the uv tool…", file=stream)
    runner([uv, "tool", "install", "--reinstall", "."], cwd=repo)


def cycle(repo: Path, host: str, port: int, *, stream: TextIO = sys.stdout) -> bool:
    """Upgrade first, then minimize downtime while replacing every same-user server."""
    managed = systemd_unit_installed()
    upgrade(repo, stream=stream)
    print("→ stopping Alexandria MCP servers on the old build…", file=stream)
    stop_all(use_systemd=managed, force=True, stream=stream)
    print("→ starting the HTTP server on the new build…", file=stream)
    healthy = start_server(repo, host, port, use_systemd=managed, stream=stream)
    print(f"Alexandria {service_version()}", file=stream)
    if healthy:
        print("Cycle complete.", file=stream)
    return healthy


def _print_status(host: str, port: int, *, stream: TextIO = sys.stdout) -> None:
    print(f"Alexandria {service_version()}", file=stream)
    processes = mcp_processes()
    if not processes:
        print("Processes: none", file=stream)
    for process in processes:
        kind = "HTTP" if process.is_http else "stdio"
        owner = "background/server" if process.tty in {"?", "??", "-"} else process.tty
        print(f"Process: pid {process.pid} · {kind} · {owner}", file=stream)
    _, detail = _health(host, port)
    print(f"Health: {detail}", file=stream)


def _print_urls(
    host: str,
    port: int,
    tunnel_path: str | None,
    tunnel_port: int | None,
    *,
    stream: TextIO = sys.stdout,
) -> None:
    from alexandria.infrastructure.config import load_config
    from alexandria.mcp_server import (
        _extra_allowed_hosts,
        _http_token,
        _tunnel_path,
        _tunnel_port,
        render_urls,
    )

    token = _http_token(load_config())
    hosts = _extra_allowed_hosts(None)
    for line in render_urls(
        token,
        hosts,
        host=host,
        port=port,
        tunnel_path=_tunnel_path(tunnel_path),
        tunnel_port=_tunnel_port(tunnel_port),
    ):
        print(line, file=stream)
    if not hosts:
        print(
            "(no tunnel hostname detected — is Tailscale up, or is ALEXANDRIA_ALLOWED_HOSTS set?)",
            file=stream,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria-ctl")
    parser.add_argument("--repo", type=Path, default=_default_repo())
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--tunnel-path", default=None)
    parser.add_argument("--tunnel-port", type=int, default=None)
    parser.add_argument(
        "command", choices=["status", "url", "start", "stop-all", "upgrade", "cycle"]
    )
    args = parser.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    try:
        if args.command == "status":
            _print_status(args.host, args.port)
        elif args.command == "url":
            _print_urls(args.host, args.port, args.tunnel_path, args.tunnel_port)
        elif args.command == "start":
            return (
                0
                if start_server(
                    repo,
                    args.host,
                    args.port,
                    use_systemd=systemd_unit_installed(),
                )
                else 1
            )
        elif args.command == "stop-all":
            stop_all(use_systemd=systemd_unit_installed())
        elif args.command == "upgrade":
            upgrade(repo)
        elif args.command == "cycle":
            return 0 if cycle(repo, args.host, args.port) else 1
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"alexandria-ctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
