import io
import os
import shutil
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


def test_upgrade_pulls_then_reinstalls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    calls: list[tuple[list[str], Path | None]] = []

    def runner(command: list[str], *, cwd: Path | None = None) -> None:
        calls.append((command, cwd))

    monkeypatch.setattr(shutil, "which", lambda name: "/tools/uv" if name == "uv" else None)

    control.upgrade(tmp_path, runner=runner, stream=io.StringIO())

    assert calls == [
        (["git", "pull", "--ff-only"], tmp_path),
        (["/tools/uv", "tool", "install", "--reinstall", "."], tmp_path),
    ]


def test_upgrade_failure_happens_before_process_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def fail_upgrade(repo: Path, *, stream: object) -> None:
        events.append("upgrade")
        raise RuntimeError("pull failed")

    monkeypatch.setattr(control, "systemd_unit_installed", lambda: False)
    monkeypatch.setattr(control, "upgrade", fail_upgrade)
    monkeypatch.setattr(
        control,
        "stop_all",
        lambda **_kwargs: events.append("stop"),
    )

    with pytest.raises(RuntimeError, match="pull failed"):
        control.cycle(tmp_path, "127.0.0.1", 8797, stream=io.StringIO())

    assert events == ["upgrade"]


def test_cycle_orders_upgrade_stop_start_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def fake_upgrade(repo: Path, *, stream: object) -> None:
        assert repo == tmp_path
        events.append("upgrade")

    def fake_stop_all(*, use_systemd: bool, force: bool, stream: object) -> int:
        assert use_systemd is True
        assert force is True
        events.append("stop")
        return 2

    def fake_start(
        repo: Path,
        host: str,
        port: int,
        *,
        use_systemd: bool,
        stream: object,
    ) -> bool:
        assert (repo, host, port, use_systemd) == (tmp_path, "127.0.0.1", 8797, True)
        events.append("start-and-health")
        return True

    monkeypatch.setattr(control, "systemd_unit_installed", lambda: True)
    monkeypatch.setattr(control, "upgrade", fake_upgrade)
    monkeypatch.setattr(control, "stop_all", fake_stop_all)
    monkeypatch.setattr(control, "start_server", fake_start)

    assert control.cycle(tmp_path, "127.0.0.1", 8797, stream=io.StringIO()) is True
    assert events == ["upgrade", "stop", "start-and-health"]


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
