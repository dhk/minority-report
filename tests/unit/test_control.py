import io
import json
import os
from pathlib import Path

import pytest

from alexandria import control
from alexandria.control import ProcessInfo
from alexandria.infrastructure import config as config_module


def _corpus(tmp_path: Path, *slugs: str) -> Path:
    repo = tmp_path / "corpus"
    for slug in slugs:
        (repo / "research" / slug).mkdir(parents=True)
    (repo / "research").mkdir(parents=True, exist_ok=True)
    return repo


def _quiet_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep _print_status off this host's real processes and network."""
    monkeypatch.setattr(control, "mcp_processes", list)
    monkeypatch.setattr(control, "_health", lambda _host, _port: (False, "not running"))


def test_status_reports_the_corpus_it_resolved_and_its_investigation_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_status(monkeypatch)
    repo = _corpus(tmp_path, "2026-07-29-one", "2026-08-01-two")
    stream = io.StringIO()

    control._print_status(
        "127.0.0.1",
        8797,
        stream=stream,
        env={"ALEXANDRIA_REPO": str(repo)},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path,
    )

    output = stream.getvalue()
    assert f"Repo: {repo}" in output
    assert "Investigations: 2" in output


def test_status_reports_an_empty_corpus_as_zero_not_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_status(monkeypatch)
    repo = _corpus(tmp_path)
    stream = io.StringIO()

    control._print_status(
        "127.0.0.1",
        8797,
        stream=stream,
        env={"ALEXANDRIA_REPO": str(repo)},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path,
    )

    assert "Investigations: 0" in stream.getvalue()


def test_status_says_the_corpus_is_unconfigured_and_still_reports_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_status(monkeypatch)
    monkeypatch.delenv("ALEXANDRIA_REPO", raising=False)
    stream = io.StringIO()

    control._print_status(
        "127.0.0.1",
        8797,
        stream=stream,
        env={},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path / "nowhere",
    )

    output = stream.getvalue()
    assert "Repo: not configured (set ALEXANDRIA_REPO)" in output
    # status is the command reached for when something is wrong: it must still
    # report process and health rather than stopping at the missing corpus.
    assert "Health:" in output


def test_a_corpus_pointer_at_a_missing_path_is_not_reported_as_an_empty_corpus(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent"

    lines = control._corpus_lines(
        {"ALEXANDRIA_REPO": str(absent)},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path,
    )

    assert lines[0] == f"Repo: {absent} — does not exist"
    assert lines[1] == "Investigations: unknown"
    assert "Investigations: 0" not in lines


def test_a_corpus_without_a_research_directory_says_so_rather_than_just_zero(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()

    lines = control._corpus_lines(
        {"ALEXANDRIA_REPO": str(repo)},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path,
    )

    assert lines == [f"Repo: {repo}", "Investigations: 0 (no research/ directory yet)"]


def _server(command: str, pid: int = 4242) -> control.ProcessInfo:
    return control.ProcessInfo(pid=pid, uid=0, tty="??", command=command)


def test_the_running_servers_flags_are_read_from_its_command_line() -> None:
    """#4: ctl re-derived --tunnel-path from its own environment and got it wrong."""
    process = _server(
        "/home/dhk/.local/bin/alexandria-mcp --http --port 8797 --tunnel-path /alexandria"
    )

    assert control.service_invocation(process) == {
        "--port": "8797",
        "--tunnel-path": "/alexandria",
    }


def test_no_running_server_means_no_flags_to_borrow() -> None:
    assert control.service_invocation(None) == {}


def test_a_trailing_flag_without_a_value_is_ignored() -> None:
    process = _server("alexandria-mcp --http --tunnel-path")

    assert control.service_invocation(process) == {}


def test_url_uses_the_tunnel_path_the_server_is_running_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "corpus"
    (repo / "research").mkdir(parents=True)
    monkeypatch.setenv("ALEXANDRIA_REPO", str(repo))
    monkeypatch.setenv("ALEXANDRIA_DATA_DIR", str(tmp_path / "data"))
    # Supply the front door rather than depending on this machine having a
    # tailnet name: without it the tunnel line is correctly absent, and the
    # test would pass on a deployed host and fail in CI.
    monkeypatch.setenv("ALEXANDRIA_ALLOWED_HOSTS", "lobster.example.ts.net")
    stream = io.StringIO()

    control._print_urls(
        "127.0.0.1",
        8797,
        None,
        None,
        stream=stream,
        process=_server("alexandria-mcp --http --port 8797 --tunnel-path /alexandria"),
    )

    output = stream.getvalue()
    assert "https://lobster.example.ts.net/alexandria/mcp/" in output
    assert "tunnel path read from the running server, pid 4242" in output


def test_an_explicit_tunnel_path_still_wins_over_the_running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator may be asking about a server that is not this one."""
    repo = tmp_path / "corpus"
    (repo / "research").mkdir(parents=True)
    monkeypatch.setenv("ALEXANDRIA_REPO", str(repo))
    monkeypatch.setenv("ALEXANDRIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALEXANDRIA_ALLOWED_HOSTS", "lobster.example.ts.net")
    stream = io.StringIO()

    control._print_urls(
        "127.0.0.1",
        8797,
        "/elsewhere",
        None,
        stream=stream,
        process=_server("alexandria-mcp --http --tunnel-path /alexandria"),
    )

    output = stream.getvalue()
    assert "https://lobster.example.ts.net/elsewhere/mcp/" in output
    assert "/alexandria/mcp/" not in output
    assert "read from the running server" not in output


def test_status_reports_the_corpus_the_server_opened_when_they_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#20's stated limitation: status reported the CLI's answer, confidently."""
    served = tmp_path / "served"
    (served / "research" / "one").mkdir(parents=True)
    cli_repo = tmp_path / "what-the-cli-thinks"
    (cli_repo / "research").mkdir(parents=True)
    process = _server("alexandria-mcp --http")
    monkeypatch.setattr(control, "service_repo", lambda _process: str(served))

    lines = control._corpus_lines(
        {"ALEXANDRIA_REPO": str(cli_repo)},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path,
        process=process,
    )

    assert lines[0] == f"Repo: {served} — as opened by the running server (pid 4242)"
    assert any("WARNING" in line and str(cli_repo) in line for line in lines)
    assert "Investigations: 1" in lines


def test_status_stays_quiet_when_the_server_and_the_cli_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "corpus"
    (repo / "research" / "one").mkdir(parents=True)
    monkeypatch.setattr(control, "service_repo", lambda _process: str(repo))

    lines = control._corpus_lines(
        {"ALEXANDRIA_REPO": str(repo)},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path,
        process=_server("alexandria-mcp --http"),
    )

    assert lines == [f"Repo: {repo}", "Investigations: 1"]
    assert not any("WARNING" in line for line in lines)


def test_a_served_corpus_that_is_gone_is_not_reported_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "service_repo", lambda _process: str(tmp_path / "absent"))

    lines = control._corpus_lines(
        {"ALEXANDRIA_REPO": str(tmp_path)},
        host_env_file=tmp_path / "missing.env",
        cwd=tmp_path,
        process=_server("alexandria-mcp --http"),
    )

    assert "Investigations: unknown (that path does not exist)" in lines
    assert not any("Investigations: 0" in line for line in lines)


_FUNNEL = {
    "Web": {
        "lobster.example.ts.net:443": {
            "Handlers": {
                "/": {"Proxy": "http://127.0.0.1:8789"},
                "/shared": {"Proxy": "http://127.0.0.1:8789"},
                "/alexandria": {"Proxy": "http://127.0.0.1:8797"},
            }
        }
    }
}


class _Serve:
    returncode = 0

    def __init__(self, payload: object) -> None:
        self.stdout = json.dumps(payload)


def _with_tailscale(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    monkeypatch.setattr("alexandria.control.shutil.which", lambda _name: "/usr/bin/tailscale")


def test_only_paths_pointing_at_our_own_port_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_tailscale(monkeypatch, _FUNNEL)

    assert control.funnel_paths_for_port(8797, runner=lambda *_a, **_k: _Serve(_FUNNEL)) == [
        "/alexandria"
    ]
    assert control.funnel_paths_for_port(8789, runner=lambda *_a, **_k: _Serve(_FUNNEL)) == [
        "/",
        "/shared",
    ]


def test_a_funnel_it_cannot_read_is_reported_as_unknown_not_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence beats asserting an absence that was never verified."""
    monkeypatch.setattr("alexandria.control.shutil.which", lambda _name: None)
    assert control.funnel_paths_for_port(8797) is None

    monkeypatch.setattr("alexandria.control.shutil.which", lambda _name: "/usr/bin/tailscale")

    class _Broken:
        returncode = 0
        stdout = "not json"

    assert control.funnel_paths_for_port(8797, runner=lambda *_a, **_k: _Broken()) is None

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("tailscaled is not answering")

    assert control.funnel_paths_for_port(8797, runner=_boom) is None


def test_advertising_a_path_the_tunnel_forwards_says_nothing() -> None:
    assert control.funnel_advice("/alexandria", ["/alexandria"]) == []


def test_advertising_the_bare_root_while_mounted_under_a_prefix_warns() -> None:
    """The original #4 failure: '/' reaches whichever service owns that path."""
    advice = control.funnel_advice("/", ["/alexandria"])

    assert advice
    assert "does not forward /" in advice[0]
    assert "/alexandria" in advice[0]


def test_a_port_the_tunnel_forwards_nothing_to_warns() -> None:
    advice = control.funnel_advice("/alexandria", [])

    assert advice and "forwards nothing to this port" in advice[0]


def test_an_unreadable_funnel_produces_no_advice() -> None:
    assert control.funnel_advice("/alexandria", None) == []


def test_default_repo_prefers_environment(tmp_path: Path) -> None:
    assert (
        control._default_repo(
            {"ALEXANDRIA_REPO": str(tmp_path)}, host_env_file=tmp_path / "missing.env"
        )
        == tmp_path.resolve()
    )


def test_default_repo_reads_canonical_host_file_from_unrelated_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "release"
    host_file = tmp_path / "alexandria.env"
    host_file.write_text(f"ALEXANDRIA_REPO={repo}\n", encoding="utf-8")

    assert control._default_repo({}, host_env_file=host_file, cwd=tmp_path / "home") == repo


def test_url_command_reads_canonical_host_file_from_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alexandria import mcp_server

    repo = tmp_path / "release"
    data_dir = tmp_path / "data"
    host_file = tmp_path / "alexandria.env"
    host_file.write_text(
        f"ALEXANDRIA_REPO={repo}\nALEXANDRIA_DATA_DIR={data_dir}\n", encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "DEFAULT_HOST_ENV_FILE", host_file)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_http_token", lambda config: "test-token")
    monkeypatch.setattr(mcp_server, "_extra_allowed_hosts", lambda _value: [])
    monkeypatch.setattr(mcp_server, "_tunnel_path", lambda value: value)
    monkeypatch.setattr(mcp_server, "_tunnel_port", lambda value: value)
    monkeypatch.setattr(mcp_server, "render_urls", lambda *_args, **_kwargs: ["local-url"])

    output = io.StringIO()
    control._print_urls("127.0.0.1", 8797, None, None, stream=output)

    assert "local-url" in output.getvalue()


def test_url_command_reports_invalid_host_file_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host_file = tmp_path / "alexandria.env"
    host_file.write_text("ALEXANDRIA_REPO\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "DEFAULT_HOST_ENV_FILE", host_file)
    monkeypatch.chdir(tmp_path)

    assert control.main(["url"]) == 1
    error = capsys.readouterr().err
    assert "expected NAME=value" in error
    assert "Traceback" not in error


def test_parse_processes_and_filter_same_user(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = os.getuid()
    processes = control._parse_processes(
        "\n".join(
            [
                f"101 {uid} ?? /opt/bin/alexandria-mcp --http",
                f"102 {uid} ttys001 uv run alexandria-mcp",
                f"103 {uid + 1} ?? /opt/bin/alexandria-mcp --http",
                f"104 {uid} ?? /opt/bin/something-else",
            ]
        )
    )
    monkeypatch.setattr(os, "getpid", lambda: 999)
    monkeypatch.setattr(os, "getppid", lambda: 998)

    found = control.mcp_processes(processes)

    assert [process.pid for process in found] == [101, 102]
    assert found[0].is_http is True
    assert found[1].is_http is False


def _checkout(root: Path, scripts: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    body = "\n".join(f'{name} = "{target}"' for name, target in scripts.items())
    (root / "pyproject.toml").write_text(
        '[project]\nname = "alexandria"\nversion = "0.1.0"\n\n[project.scripts]\n' + body + "\n"
    )
    return root


def test_upgrade_refuses_the_corpus_checkout(tmp_path: Path) -> None:
    """The corpus declares the same package name and no entry points (#63)."""
    corpus = tmp_path / "alexandria-corpus"
    corpus.mkdir()
    (corpus / ".git").mkdir()
    (corpus / "pyproject.toml").write_text('[project]\nname = "alexandria"\nversion = "0.1.0"\n')

    with pytest.raises(RuntimeError, match="alexandria-mcp"):
        control.assert_installable_source(corpus)


def test_upgrade_accepts_the_tooling_checkout(tmp_path: Path) -> None:
    source = _checkout(
        tmp_path / "minority-report",
        {
            "alexandria-mcp": "alexandria.mcp_server:main",
            "alexandria-web": "alexandria.web:main",
            "alexandria-ctl": "alexandria.control:main",
        },
    )
    control.assert_installable_source(source)


def test_upgrade_refuses_a_tree_that_is_not_a_checkout(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not a git checkout"):
        control.assert_installable_source(tmp_path)


def test_deploying_a_branch_is_a_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(control, "_git", lambda args, cwd: _fake_git(args, branch="feat/x"))
    with pytest.raises(RuntimeError, match="not main"):
        control._source_commit(tmp_path, None, force=False)
    # named explicitly, it is allowed
    assert control._source_commit(tmp_path, "feat/x", force=False) == "abc123def456"


def test_a_dirty_tree_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(control, "_git", lambda args, cwd: _fake_git(args, dirty=True))
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        control._source_commit(tmp_path, None, force=False)
    assert control._source_commit(tmp_path, None, force=True) == "abc123def456"


def _fake_git(arguments: list[str], *, branch: str = "main", dirty: bool = False) -> str:
    if arguments[:2] == ["rev-parse", "--abbrev-ref"]:
        return branch
    if arguments[0] == "status":
        return " M src/alexandria/control.py" if dirty else ""
    if arguments[0] == "rev-parse":
        return "abc123def456"
    return ""


def test_upgrade_stops_when_the_deployed_commit_already_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _checkout(tmp_path / "src", {name: "x:main" for name in control.REQUIRED_SCRIPTS})
    monkeypatch.setattr(control, "_git", lambda args, cwd: _fake_git(args))
    monkeypatch.setattr(control, "deployed_release", lambda: {"source": {"commit": "abc123def456"}})
    monkeypatch.setattr(control, "deployed_summary", lambda: "0.1.0-abc123def456-x")
    # The server is the authority on what is running; here it agrees.
    monkeypatch.setattr(
        control, "served_build", lambda host, port: "0.1.0-abc123def456-x from abc123def456"
    )
    called: list[str] = []
    monkeypatch.setattr(control, "stop_all", lambda **_k: called.append("stop"))

    stream = io.StringIO()
    assert control.upgrade(source, stream=stream) is True
    assert "Already current" in stream.getvalue()
    assert called == []


def test_ALEXANDRIA_SOURCE_is_not_the_corpus_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALEXANDRIA_REPO names the corpus; using it to install is the #63 defect."""
    assert control._default_source({"ALEXANDRIA_SOURCE": "/srv/tooling"}) == Path("/srv/tooling")
    assert control._default_source({"ALEXANDRIA_REPO": "/srv/corpus"}) != Path("/srv/corpus")


def test_cycle_delegates_to_upgrade(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[str, int]] = []

    def fake_upgrade(*, host: str, port: int, stream: object) -> bool:
        seen.append((host, port))
        return True

    monkeypatch.setattr(control, "upgrade", fake_upgrade)
    assert control.cycle(tmp_path, "127.0.0.1", 8797, stream=io.StringIO()) is True
    assert seen == [("127.0.0.1", 8797)]


def test_process_match_requires_executable_token() -> None:
    assert control._is_alexandria_mcp("/opt/bin/alexandria-mcp --http") is True
    assert control._is_alexandria_mcp("uv run alexandria-mcp") is True
    assert control._is_alexandria_mcp("echo alexandria-mcp-debug") is False


def test_process_info_distinguishes_http_from_stdio() -> None:
    http = ProcessInfo(1, 1, "??", "/bin/alexandria-mcp --http")
    stdio = ProcessInfo(2, 1, "ttys001", "/bin/alexandria-mcp")
    assert http.is_http is True
    assert stdio.is_http is False


def test_stop_all_refuses_client_processes_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[int] = []
    process = ProcessInfo(123, os.getuid(), "ttys001", "/bin/alexandria-mcp")
    monkeypatch.setattr(control, "mcp_processes", lambda: [process])

    def fake_stop(candidate: ProcessInfo) -> str:
        stopped.append(candidate.pid)
        return "stopped"

    monkeypatch.setattr(control, "_stop_process", fake_stop)
    output = io.StringIO()

    result = control.stop_all(
        use_systemd=False,
        stream=output,
        input_fn=lambda _prompt: "n",
    )

    assert result == 0
    assert stopped == []
    assert "aborted" in output.getvalue()


def test_the_server_is_asked_what_it_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """deployed_summary() describes the caller; only /health describes the service."""
    source = _checkout(tmp_path / "src", {name: "x:main" for name in control.REQUIRED_SCRIPTS})
    monkeypatch.setattr(control, "_git", lambda args, cwd: _fake_git(args))
    # This process is a checkout, not the installed tool -- the old failure mode.
    monkeypatch.setattr(control, "deployed_release", lambda: None)
    monkeypatch.setattr(control, "deployed_summary", lambda: "0.1.0 (not a pack install)")
    monkeypatch.setattr(
        control, "served_build", lambda host, port: "0.1.0-abc123def456-z from abc123def456"
    )
    called: list[str] = []
    monkeypatch.setattr(control, "stop_all", lambda **_k: called.append("stop"))

    stream = io.StringIO()
    assert control.upgrade(source, stream=stream) is True
    assert "Already current" in stream.getvalue()
    assert "not a pack install" not in stream.getvalue()
    assert called == []
