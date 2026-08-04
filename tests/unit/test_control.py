import io
import os
import shutil
from pathlib import Path

import pytest

from alexandria import control
from alexandria.control import ProcessInfo
from alexandria.infrastructure import config as config_module


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
