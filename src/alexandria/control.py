"""Local process lifecycle and upgrade command for Alexandria MCP servers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from alexandria.infrastructure.config import ENV_REPO_ROOT, RepoNotFoundError, load_config
from alexandria.infrastructure.research_repo import list_investigations
from alexandria.version import deployed_release, deployed_summary

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


def _default_repo(
    env: dict[str, str] | os._Environ[str] | None = None,
    *,
    host_env_file: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    try:
        return load_config(env, cwd=cwd, host_env_file=host_env_file).repo_root.resolve()
    except RepoNotFoundError:
        pass
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


def running_service(
    processes: list[ProcessInfo] | None = None,
) -> ProcessInfo | None:
    """This account's running HTTP server, if there is one."""
    return next((process for process in mcp_processes(processes) if process.is_http), None)


def service_invocation(process: ProcessInfo | None) -> dict[str, str]:
    """The flags the running server was actually started with.

    ``alexandria-ctl`` is a different process from the server, so re-deriving
    ``--tunnel-path``/``--port`` from its own environment produces a confident
    description of a service that is not the one running. That is #4: the unit
    passes ``--tunnel-path /alexandria``, an interactive ``url`` saw nothing,
    and the URL it printed reached a different service entirely.
    """
    if process is None:
        return {}
    tokens = process.command.split()
    found: dict[str, str] = {}
    for flag in ("--port", "--tunnel-path", "--tunnel-port", "--host"):
        if flag in tokens:
            index = tokens.index(flag)
            if index + 1 < len(tokens):
                found[flag] = tokens[index + 1]
    return found


def service_repo(process: ProcessInfo | None) -> str | None:
    """The corpus the running server actually opened, when it can be read.

    Read from the process's own environment. ``/proc`` is Linux-only and
    readable here only because the server runs as this same account; anywhere
    else this returns None and callers fall back to their own resolution
    rather than asserting something they cannot see.
    """
    if process is None:
        return None
    try:
        raw = Path(f"/proc/{process.pid}/environ").read_bytes()
    except OSError:
        return None
    for entry in raw.split(b"\0"):
        name, separator, value = entry.decode("utf-8", "replace").partition("=")
        if separator and name == ENV_REPO_ROOT and value:
            return value
    return None


def funnel_paths_for_port(
    port: int,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> list[str] | None:
    """Paths the tunnel actually forwards to this port, or None if unknowable.

    Strictly diagnostic: it reads ``tailscale serve status`` and never changes
    a mount. The funnel is shared with other services, and #4 is explicit that
    this must not touch it.

    None means "could not tell" — no tailscale, a daemon that will not answer,
    unparseable output. A tool that cannot see the funnel should say nothing
    about it rather than assert an absence it did not verify.
    """
    if shutil.which("tailscale") is None:
        return None
    try:
        result = runner(
            ["tailscale", "serve", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(result, "returncode", 1) != 0 or not isinstance(payload, dict):
        return None
    web = payload.get("Web")
    if not isinstance(web, dict):
        return []
    target = f"http://127.0.0.1:{port}"
    paths: list[str] = []
    for site in web.values():
        handlers = (site or {}).get("Handlers") if isinstance(site, dict) else None
        if not isinstance(handlers, dict):
            continue
        for path, handler in handlers.items():
            if isinstance(handler, dict) and handler.get("Proxy") == target:
                paths.append(str(path))
    return sorted(set(paths))


def funnel_advice(advertised: str, mounted: list[str] | None) -> list[str]:
    """Say whether the advertised path is one the tunnel actually forwards.

    The failure this exists for: a URL that resolves, reaches the tunnel, and
    is proxied to a *different service*, which 502s on an MCP request it knows
    nothing about — while every Alexandria diagnostic reports healthy.
    """
    if mounted is None:
        return []
    normalized = "/" + advertised.strip("/") if advertised.strip("/") else "/"
    # '/' is checked like any other path. Exempting it would silence exactly
    # the case this exists for: advertising the bare root while the funnel
    # mounts this service under a prefix and gives '/' to someone else.
    if normalized in mounted:
        return []
    if not mounted:
        return [
            (
                f"WARNING: the tunnel forwards nothing to this port, so {normalized} "
                "is not reachable through it."
            )
        ]
    return [
        (
            f"WARNING: the tunnel does not forward {normalized} to this port. "
            f"It forwards {', '.join(mounted)}."
        ),
        (
            "  A connector on that URL reaches whatever else owns the path, which will "
            "fail on a request it does not recognise while this server stays healthy."
        ),
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


DEFAULT_SOURCE = Path("~/src/minority-report")
#: Every service unit runs one of these. A source tree that does not declare
#: them is not this project, whatever its package is called.
REQUIRED_SCRIPTS = ("alexandria-mcp", "alexandria-web", "alexandria-ctl")


def _default_source(env: dict[str, str] | os._Environ[str] | None = None) -> Path:
    """Where the tooling is checked out.

    Deliberately not ALEXANDRIA_REPO. That names the corpus this service reads,
    and pointing an install at it is how `upgrade` came to reinstall the tool
    from a package that declares no executables (#63).
    """
    environment = os.environ if env is None else env
    configured = environment.get("ALEXANDRIA_SOURCE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_SOURCE.expanduser().resolve()


def _git(arguments: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def assert_installable_source(source: Path) -> None:
    """Refuse a tree that cannot produce this service.

    The corpus and this repository both declare `name = "alexandria"` at the
    same version, so a name check proves nothing. The entry points are what
    differ, and they are what the units invoke.
    """
    if not (source / ".git").is_dir():
        raise RuntimeError(f"{source} is not a git checkout; set ALEXANDRIA_SOURCE.")
    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        raise RuntimeError(f"{source} has no pyproject.toml.")
    declared = tomllib.loads(pyproject.read_text()).get("project", {}).get("scripts", {})
    missing = [name for name in REQUIRED_SCRIPTS if name not in declared]
    if missing:
        raise RuntimeError(
            f"{source} declares no {', '.join(missing)} entry point(s), so installing it "
            "would leave the service units pointing at commands that do not exist. "
            "This is the corpus checkout, not the tooling one (#63)."
        )


def _source_commit(source: Path, ref: str | None, *, force: bool) -> str:
    """The commit to deploy, refusing anything unintended unless forced."""
    if ref:
        return _git(["rev-parse", ref], source)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], source)
    dirty = bool(_git(["status", "--porcelain"], source))
    if not force:
        if branch != "main":
            raise RuntimeError(
                f"{source} is on {branch!r}, not main. Deploying a branch is a decision: "
                "pass --ref to name it, or --force to deploy this checkout as it stands."
            )
        if dirty:
            raise RuntimeError(f"{source} has uncommitted changes; commit, stash, or --force.")
    return _git(["rev-parse", "HEAD"], source)


def upgrade(
    source: Path | None = None,
    *,
    ref: str | None = None,
    force: bool = False,
    restart: bool = True,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    stream: TextIO = sys.stdout,
) -> bool:
    """Build the current source into a release, install it, and restart.

    One command, no sudo. The host wiring that needs root — the service
    registry and the tailscale route — is re-asserted only on a first install;
    an upgrade re-uses entries that are already reserved, so this takes
    install.py's --skip-service path and restarts the user units itself.

    That path also skips install.py's service health checks, so this polls
    /health afterwards rather than assuming the restart worked.
    """
    source = (source or _default_source()).expanduser().resolve()
    assert_installable_source(source)

    print(f"→ fetching {source}…", file=stream)
    _git(["fetch", "--quiet", "origin"], source)
    commit = _source_commit(source, ref, force=force)

    manifest = deployed_release() or {}
    running = (manifest.get("source") or {}).get("commit")
    print(f"  running: {deployed_summary()}", file=stream)
    print(f"  source:  {commit[:12]}", file=stream)
    if running == commit and not force:
        print("Already current; nothing to install.", file=stream)
        return True

    with tempfile.TemporaryDirectory(prefix="alexandria-upgrade-") as workspace:
        staging = Path(workspace)
        print("→ building a release bundle…", file=stream)
        subprocess.run(
            [sys.executable, "-m", "scripts.pack", "--output-dir", str(staging)],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        )
        archives = sorted(staging.glob("*.tar.gz"))
        if len(archives) != 1:
            raise RuntimeError(f"expected one bundle in {staging}, found {len(archives)}")
        archive = archives[0]

        expected = Path(f"{archive}.sha256").read_text().split()[0]
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if expected != actual:
            raise RuntimeError(f"{archive.name}: checksum mismatch; refusing to install it")
        print(f"  {archive.name} verified", file=stream)

        with tarfile.open(archive) as bundle:
            bundle.extractall(staging, filter="data")
        installer = staging / archive.name[: -len(".tar.gz")] / "install.py"
        if not installer.is_file():
            raise RuntimeError(f"bundle contains no install.py at {installer}")

        print("→ installing…", file=stream)
        subprocess.run(
            [sys.executable, str(installer), "--yes", "--skip-service"],
            check=True,
        )

    if not restart:
        print("Installed; services not restarted (--no-restart).", file=stream)
        return True

    print("→ restarting services…", file=stream)
    stop_all(use_systemd=systemd_unit_installed(), force=True, stream=stream)
    healthy = start_server(source, host, port, use_systemd=systemd_unit_installed(), stream=stream)
    print(f"Now serving {deployed_summary()}", file=stream)
    if not healthy:
        print(
            "The server did not come back healthy. The previous release is still under "
            "releases/; repoint `current` at it and reinstall to go back.",
            file=stream,
        )
    return healthy


def cycle(repo: Path, host: str, port: int, *, stream: TextIO = sys.stdout) -> bool:
    """Kept as a name: upgrade now installs and restarts in one pass."""
    healthy = upgrade(host=host, port=port, stream=stream)
    if healthy:
        print("Cycle complete.", file=stream)
    return healthy


def _served_corpus_lines(
    served: str,
    process: ProcessInfo | None,
    *,
    cli_answer: Path | None,
) -> list[str]:
    """Report the corpus the running server opened, and any disagreement.

    A disagreement is not a footnote: it means every other line of this
    command describes a corpus nobody is serving.
    """
    pid = f" (pid {process.pid})" if process is not None else ""
    lines = [f"Repo: {served} — as opened by the running server{pid}"]
    if cli_answer is not None:
        lines.append(f"  WARNING: this command's own environment resolves {cli_answer} instead.")
        lines.append("  The server is the authority; the difference is worth resolving.")
    served_path = Path(served)
    if not served_path.is_dir():
        lines.append("Investigations: unknown (that path does not exist)")
        return lines
    research = served_path / "research"
    if not research.is_dir():
        lines.append("Investigations: 0 (no research/ directory yet)")
        return lines
    try:
        lines.append(f"Investigations: {sum(1 for entry in research.iterdir() if entry.is_dir())}")
    except OSError as exc:
        lines.append(f"Investigations: unreadable ({exc})")
    return lines


def _corpus_lines(
    env: dict[str, str] | os._Environ[str] | None = None,
    *,
    host_env_file: Path | None = None,
    cwd: Path | None = None,
    process: ProcessInfo | None = None,
) -> list[str]:
    """Describe the corpus this CLI resolves, without ever raising.

    ``status`` is the command reached for when something looks wrong, so a
    corpus that cannot be resolved or read is reported on the line rather than
    ending the run.

    When a server is running and its environment is readable, that is the
    corpus reported: it is the one actually being served. The CLI's own
    resolution is reported alongside it only when the two disagree, because a
    disagreement is the interesting case and silence about it is what made
    ``url`` confidently wrong (#4).
    """
    served = service_repo(process)
    try:
        config = load_config(env, cwd=cwd, host_env_file=host_env_file)
    except RepoNotFoundError:
        if served:
            return _served_corpus_lines(served, process, cli_answer=None)
        return [f"Repo: not configured (set {ENV_REPO_ROOT})"]
    except OSError as exc:
        return [f"Repo: unreadable ({exc})"]
    if served and Path(served).resolve() != config.repo_root.resolve():
        return _served_corpus_lines(served, process, cli_answer=config.repo_root)
    repo = config.repo_root
    if not repo.is_dir():
        # Distinguished from an empty corpus deliberately: a pointer at a path
        # that is not there is the failure this line exists to catch, and
        # "0" would read as a corpus that merely has nothing in it.
        return [f"Repo: {repo} — does not exist", "Investigations: unknown"]
    if not config.research_dir.is_dir():
        return [f"Repo: {repo}", "Investigations: 0 (no research/ directory yet)"]
    try:
        count = len(list_investigations(config))
    except OSError as exc:
        return [f"Repo: {repo}", f"Investigations: unreadable ({exc})"]
    return [f"Repo: {repo}", f"Investigations: {count}"]


def _print_status(
    host: str,
    port: int,
    *,
    stream: TextIO = sys.stdout,
    env: dict[str, str] | os._Environ[str] | None = None,
    host_env_file: Path | None = None,
    cwd: Path | None = None,
) -> None:
    print(f"Alexandria {deployed_summary()}", file=stream)
    processes = mcp_processes()
    server = next((item for item in processes if item.is_http), None)
    for line in _corpus_lines(env, host_env_file=host_env_file, cwd=cwd, process=server):
        print(line, file=stream)
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
    repo: Path | None = None,
    stream: TextIO = sys.stdout,
    process: ProcessInfo | None = None,
) -> None:
    from alexandria.mcp_server import (
        _extra_allowed_hosts,
        _http_token,
        _tunnel_path,
        _tunnel_port,
        render_urls,
    )

    # What the server is actually serving beats what this process would guess.
    # An explicit flag still wins over both -- the operator may be asking about
    # a server that is not running yet.
    invocation = service_invocation(process)
    described = ""
    if tunnel_path is None and "--tunnel-path" in invocation:
        tunnel_path = invocation["--tunnel-path"]
        described = f"pid {process.pid}" if process is not None else ""
    if tunnel_port is None and "--tunnel-port" in invocation:
        tunnel_port = int(invocation["--tunnel-port"])

    environment = None
    if repo is not None:
        environment = dict(os.environ)
        environment[ENV_REPO_ROOT] = str(repo.expanduser().resolve())
    token = _http_token(load_config(environment))
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
    if described:
        print(f"(tunnel path read from the running server, {described})", file=stream)
    if hosts:
        # Only worth checking when a tunnel URL was actually printed: without a
        # front-door host there is no tunnel claim to be wrong about.
        for line in funnel_advice(_tunnel_path(tunnel_path) or "/", funnel_paths_for_port(port)):
            print(line, file=stream)
    if not hosts:
        print(
            "(no tunnel hostname detected — is Tailscale up, or is ALEXANDRIA_ALLOWED_HOSTS set?)",
            file=stream,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria-ctl")
    parser.add_argument("--repo", type=Path, help="corpus checkout this service reads")
    parser.add_argument("--source", type=Path, help="tooling checkout to build from")
    parser.add_argument("--ref", help="git ref to deploy instead of the checked-out main")
    parser.add_argument("--force", action="store_true", help="deploy a dirty or non-main tree")
    parser.add_argument("--no-restart", action="store_true", help="install without restarting")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--tunnel-path", default=None)
    parser.add_argument("--tunnel-port", type=int, default=None)
    parser.add_argument(
        "command", choices=["status", "url", "start", "stop-all", "upgrade", "cycle"]
    )
    args = parser.parse_args(argv)
    try:
        repo = args.repo.expanduser().resolve() if args.repo else None
        if args.command in {"start", "cycle"} and repo is None:
            repo = _default_repo()
        if args.command == "status":
            _print_status(args.host, args.port)
        elif args.command == "url":
            _print_urls(
                args.host,
                args.port,
                args.tunnel_path,
                args.tunnel_port,
                repo=args.repo,
                process=running_service(),
            )
        elif args.command == "start":
            assert repo is not None
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
            return (
                0
                if upgrade(
                    args.source,
                    ref=args.ref,
                    force=args.force,
                    restart=not args.no_restart,
                    host=args.host,
                    port=args.port,
                )
                else 1
            )
        elif args.command == "cycle":
            assert repo is not None
            return 0 if cycle(repo, args.host, args.port) else 1
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"alexandria-ctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
