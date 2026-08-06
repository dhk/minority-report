from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from typing import Self

import pytest

import deploy.install as installer
from deploy.checks import (
    _http_check,
    required_checks_pass,
    run_component_checks,
    service_registry_state,
    tailscale_route_state,
)
from deploy.install import (
    InstallError,
    _configure_tailscale,
    _install_registry_helper,
    _print_capability,
    _registry_commands,
    _resolve_external_repo,
    _tailscale_command,
    _write_units,
    bundle_cleanup_targets,
    cleanup_bundle_artifacts,
    ensure_environment_file,
    ensure_secrets,
    existing_environment_value,
    install_root_needs_adoption,
    install_support,
    mark_install_root,
    prompt_install_root,
    render_service_unit,
    resolve_install_root,
)
from scripts.pack import (
    PackError,
    PackSpec,
    ServiceSpec,
    build_bundle,
    load_spec,
    source_files,
)

ROOT = Path(__file__).resolve().parents[2]


class _HealthResponse:
    status = 200

    def __init__(self, service: str) -> None:
        self.service = service

    def read(self, _limit: int = -1) -> bytes:
        return json.dumps({"service": self.service, "version": "test"}).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tool"
    repo.mkdir()
    (repo / "deploy").mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "sample-tool"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (repo / "README.md").write_text("committed\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\ndist/\n", encoding="utf-8")
    (repo / "deploy/install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "deploy/docs.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "deploy/checks.py").write_text(
        (ROOT / "deploy/checks.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "pack@example.test")
    _git(repo, "config", "user.name", "Pack Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _spec() -> PackSpec:
    return PackSpec(
        format_version=1,
        name="sample-tool",
        display_name="Sample Tool",
        default_install_root="~/.local/opt/sample-tool",
        repo_environment="SAMPLE_REPO",
        environment_file="~/.config/sample-tool/sample-tool.env",
        environment={"SAMPLE_DATA_DIR": "~/.local/share/sample-tool"},
        secrets_file="~/.config/sample-tool/secrets.env",
        required_secrets=["SAMPLE_API_KEY"],
        exclude=[],
        services=[
            ServiceSpec(
                unit="sample-tool.service",
                description="Sample Tool",
                entrypoint="sample-tool",
                args=["serve"],
                health_url="http://127.0.0.1:9000/health",
                health_service="sample-tool",
            )
        ],
        capability=None,
        tailscale=None,
        registry=None,
    )


def test_alexandria_pack_config_is_valid() -> None:
    spec = load_spec(ROOT / "deploy/pack.toml")

    assert spec.name == "alexandria"
    assert spec.default_install_root == "~/src/alexandria"
    assert spec.environment_file == "~/.config/alexandria/alexandria.env"
    assert spec.environment["ALEXANDRIA_SECRETS_FILE"] == ("~/.config/alexandria/secrets.env")
    assert spec.required_secrets == ["OPENROUTER_API_KEY"]
    assert spec.services[0].unit == "alexandria-mcp.service"
    assert spec.services[0].args == [
        "--http",
        "--port",
        "8797",
        "--tunnel-path",
        "/alexandria",
    ]
    assert spec.services[0].health_url == "http://127.0.0.1:8797/health"
    assert spec.services[0].health_service == "alexandria"
    assert spec.tailscale is not None
    assert spec.tailscale.mode == "funnel"
    assert spec.tailscale.path == "/alexandria"
    assert spec.tailscale.target == "http://127.0.0.1:8797"
    assert spec.registry is not None
    assert spec.registry.helper_path == "/usr/local/bin/service-registry"
    assert spec.registry.static_range == [8700, 8799]
    assert spec.registry.dynamic_range == [8800, 8999]
    assert [entry.service_id for entry in spec.registry.entries] == [
        "wingman",
        "wingman-trent",
        "alexandria",
    ]
    assert spec.registry.entries[1].owner == "trent"
    assert spec.registry.entries[2].route is not None
    assert spec.registry.entries[2].route.path == "/alexandria"


def test_health_checks_reject_another_service_on_the_expected_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _HealthResponse("wingman")
    monkeypatch.setattr("deploy.install.urllib.request.urlopen", lambda *_args, **_kwargs: response)
    monotonic = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("deploy.install.time.monotonic", lambda: next(monotonic))
    monkeypatch.setattr("deploy.install.time.sleep", lambda _seconds: None)

    assert installer._healthy("http://127.0.0.1:8797/health", "alexandria", 1.0) is False
    panel = _http_check("http://127.0.0.1:8797/health", "alexandria", 1.0)
    assert panel.state == "fail"
    assert "expected 'alexandria', received 'wingman'" in panel.detail


def test_health_checks_accept_alexandria(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _HealthResponse("alexandria")
    monkeypatch.setattr("deploy.install.urllib.request.urlopen", lambda *_args, **_kwargs: response)

    assert installer._healthy("http://127.0.0.1:8797/health", "alexandria", 1.0) is True
    assert _http_check("http://127.0.0.1:8797/health", "alexandria", 1.0).state == "pass"


def test_tailscale_route_check_matches_path_target_and_funnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "Web": {
            "lobster.tail.ts.net:443": {
                "Handlers": {"/alexandria": {"Proxy": "http://127.0.0.1:8797"}}
            }
        },
        "AllowFunnel": {"lobster.tail.ts.net:443": True},
    }
    completed = subprocess.CompletedProcess(["tailscale"], 0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed)

    state, detail = tailscale_route_state(
        {
            "mode": "funnel",
            "path": "/alexandria",
            "target": "http://127.0.0.1:8797",
            "port": 443,
        }
    )

    assert state == "pass"
    assert "lobster.tail.ts.net:443/alexandria" in detail


def test_tailscale_route_check_reports_conflicting_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "Web": {
            "lobster.tail.ts.net:443": {
                "Handlers": {"/alexandria": {"Proxy": "http://127.0.0.1:9999"}}
            }
        }
    }
    completed = subprocess.CompletedProcess(["tailscale"], 0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed)

    state, detail = tailscale_route_state(
        {
            "mode": "funnel",
            "path": "/alexandria",
            "target": "http://127.0.0.1:8797",
            "port": 443,
        }
    )

    assert state == "conflict"
    assert "9999" in detail


def test_tailscale_command_adds_only_the_declared_path() -> None:
    assert _tailscale_command(
        {
            "mode": "funnel",
            "path": "/alexandria",
            "target": "http://127.0.0.1:8797",
            "port": 443,
        }
    ) == [
        "sudo",
        "tailscale",
        "funnel",
        "--yes",
        "--bg",
        "--set-path",
        "/alexandria",
        "http://127.0.0.1:8797",
    ]


def test_tailscale_setup_refuses_noninteractive_route_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {
        "tailscale": {
            "mode": "funnel",
            "path": "/alexandria",
            "target": "http://127.0.0.1:8797",
            "port": 443,
            "required": True,
        }
    }
    monkeypatch.setattr(
        installer,
        "tailscale_route_state",
        lambda _config: ("conflict", "/alexandria maps to a different service"),
    )
    commands: list[list[str]] = []

    with pytest.raises(InstallError, match="refusing to replace"):
        _configure_tailscale(
            manifest,
            interactive=False,
            input_fn=lambda _prompt: "",
            runner=lambda command: commands.append(list(command)),
        )

    assert commands == []


def test_tailscale_setup_adds_and_verifies_only_a_missing_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {
        "tailscale": {
            "mode": "funnel",
            "path": "/alexandria",
            "target": "http://127.0.0.1:8797",
            "port": 443,
            "required": True,
        }
    }
    states = iter(
        [
            ("missing", "not configured"),
            ("pass", "https://lobster.tail.ts.net:443/alexandria is ready"),
        ]
    )
    monkeypatch.setattr(installer, "tailscale_route_state", lambda _config: next(states))
    commands: list[list[str]] = []

    _configure_tailscale(
        manifest,
        interactive=False,
        input_fn=lambda _prompt: "",
        runner=lambda command: commands.append(list(command)),
    )

    assert commands == [_tailscale_command(manifest["tailscale"])]


def test_registry_commands_reserve_endpoint_before_external_route() -> None:
    config = {
        "helper_path": "/usr/local/bin/service-registry",
        "data_path": "/var/lib/common-services/registry.json",
        "static_range": [8700, 8799],
        "dynamic_range": [8800, 8999],
        "entries": [
            {
                "service_id": "alexandria",
                "display_name": "Alexandria",
                "owner": "dhk",
                "protocol": "tcp",
                "address": "127.0.0.1",
                "port": 8797,
                "unit": "alexandria-mcp.service",
                "health_url": "http://127.0.0.1:8797/health",
                "health_service": "alexandria",
                "source": "dhk/alexandria",
                "adopt_listener": True,
                "route": {
                    "mode": "funnel",
                    "host": "tailscale-self",
                    "https_port": 443,
                    "path": "/alexandria",
                    "target": "http://127.0.0.1:8797",
                },
            }
        ],
    }

    commands = _registry_commands(config)

    assert commands[0][-1] == "--adopt-listener"
    assert "reserve" in commands[0]
    assert "reserve-route" in commands[1]
    assert commands[1][-1] == "http://127.0.0.1:8797"


def test_registry_helper_refuses_unknown_noninteractive_replacement(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "deploy").mkdir(parents=True)
    (bundle / "deploy/service_registry.py").write_text(
        "#!/usr/bin/env python3\n# common-services-registry-format: 1\n",
        encoding="utf-8",
    )
    target = tmp_path / "bin/service-registry"
    target.parent.mkdir()
    target.write_text("unknown helper\n", encoding="utf-8")
    commands: list[list[str]] = []

    with pytest.raises(InstallError, match="unknown shared registry helper"):
        _install_registry_helper(
            bundle,
            {"helper_path": str(target)},
            interactive=False,
            input_fn=lambda _prompt: "",
            runner=lambda command: commands.append(list(command)),
        )

    assert commands == []


def test_registry_component_check_matches_declared_reservations(tmp_path: Path) -> None:
    helper = tmp_path / "service-registry"
    helper.write_text("helper\n", encoding="utf-8")
    data = tmp_path / "registry.json"
    data.write_text(
        json.dumps(
            {
                "format_version": 1,
                "services": {
                    "alexandria": {
                        "endpoint": {
                            "protocol": "tcp",
                            "address": "127.0.0.1",
                            "port": 8797,
                        },
                        "route": {
                            "mode": "funnel",
                            "host": "tailscale-self",
                            "https_port": 443,
                            "path": "/alexandria",
                            "target": "http://127.0.0.1:8797",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = {
        "helper_path": str(helper),
        "data_path": str(data),
        "entries": [
            {
                "service_id": "alexandria",
                "protocol": "tcp",
                "address": "127.0.0.1",
                "port": 8797,
                "route": {
                    "mode": "funnel",
                    "host": "tailscale-self",
                    "https_port": 443,
                    "path": "/alexandria",
                    "target": "http://127.0.0.1:8797",
                },
            }
        ],
    }

    ready, detail = service_registry_state(config)

    assert ready is True
    assert "1 declared services" in detail


def test_capability_urls_resolve_tailscale_dns_without_exposing_token_in_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    token_file = home / ".local/share/sample/token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("SECRET-TOKEN\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    manifest = {
        "capability": {
            "token_file": "~/.local/share/sample/token",
            "urls": ["https://{tailscale_dns}/sample/mcp/{token}"],
        }
    }

    _print_capability(manifest, tailscale_dns=lambda: "lobster.tail.ts.net")

    assert "https://lobster.tail.ts.net/sample/mcp/SECRET-TOKEN" in capsys.readouterr().out


def test_source_set_includes_worktree_files_but_not_ignored_or_secret_files(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("working tree\n", encoding="utf-8")
    (repo / "notes.txt").write_text("include me\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignore me\n", encoding="utf-8")
    (repo / "secrets.env").write_text("SAMPLE_API_KEY=secret\n", encoding="utf-8")

    files = source_files(repo, [])

    assert Path("README.md") in files
    assert Path("notes.txt") in files
    assert Path("ignored.txt") not in files
    assert Path("secrets.env") not in files


def test_build_bundle_contains_manifest_installer_and_current_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("working tree\n", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")

    result = build_bundle(repo, _spec(), tmp_path / "output")

    assert result.archive.is_file()
    assert result.checksum.is_file()
    with tarfile.open(result.archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        readme_name = f"{result.bundle_root}/source/README.md"
        assert f"{result.bundle_root}/install.py" in names
        assert f"{result.bundle_root}/launch-docs.py" in names
        assert f"{result.bundle_root}/deploy/__init__.py" in names
        assert f"{result.bundle_root}/deploy/checks.py" in names
        assert f"{result.bundle_root}/docs-index.html" in names
        assert f"{result.bundle_root}/pack-manifest.json" in names
        assert f"{result.bundle_root}/source/notes.txt" in names
        extracted = bundle.extractfile(readme_name)
        assert extracted is not None
        assert extracted.read() == b"working tree\n"


def test_build_bundle_requires_opt_in_for_large_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "large.bin").write_bytes(b"x" * 32)

    with pytest.raises(PackError, match="exceed the size limit"):
        build_bundle(repo, _spec(), tmp_path / "output", max_file_bytes=16)


def test_ensure_secrets_uses_environment_without_echoing_value(tmp_path: Path) -> None:
    path = tmp_path / ".config" / "sample" / "secrets.env"

    unresolved = ensure_secrets(
        path,
        ["SAMPLE_API_KEY"],
        environ={"SAMPLE_API_KEY": "secret-value"},
        interactive=False,
    )

    assert unresolved == []
    assert path.read_text(encoding="utf-8") == "SAMPLE_API_KEY=secret-value\n"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_ensure_secrets_never_replaces_an_existing_value(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("SAMPLE_API_KEY=original\n", encoding="utf-8")

    unresolved = ensure_secrets(
        path,
        ["SAMPLE_API_KEY"],
        environ={"SAMPLE_API_KEY": "replacement"},
        interactive=False,
    )

    assert unresolved == []
    assert path.read_text(encoding="utf-8") == "SAMPLE_API_KEY=original\n"


def test_existing_install_root_must_be_adopted_and_is_never_cleared(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    existing = root / "keep.txt"
    existing.write_text("keep me\n", encoding="utf-8")

    assert install_root_needs_adoption(root, "sample-tool") is True
    mark_install_root(root, "sample-tool")

    assert install_root_needs_adoption(root, "sample-tool") is False
    assert existing.read_text(encoding="utf-8") == "keep me\n"


# A password-shaped answer: no separator, so the old code silently resolved it
# against the working directory and made it a real install root.
_SECRET_SHAPED = "hunter2Passw0rd"


def test_install_root_refuses_a_relative_answer_instead_of_resolving_it_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workdir = tmp_path / "some-unrelated-repo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    with pytest.raises(InstallError) as excinfo:
        resolve_install_root(_SECRET_SHAPED, bundle_root=bundle)

    assert "absolute" in str(excinfo.value)
    assert not (workdir / _SECRET_SHAPED).exists()
    assert list(workdir.iterdir()) == []


def test_install_root_rejection_never_echoes_the_rejected_answer(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    with pytest.raises(InstallError) as excinfo:
        resolve_install_root(_SECRET_SHAPED, bundle_root=bundle)

    assert _SECRET_SHAPED not in str(excinfo.value)


def test_install_root_refuses_a_path_inside_the_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "releases").mkdir(parents=True)

    with pytest.raises(InstallError, match="inside the bundle"):
        resolve_install_root(str(bundle / "releases" / "oops"), bundle_root=bundle)

    with pytest.raises(InstallError, match="inside the bundle"):
        resolve_install_root(str(bundle), bundle_root=bundle)


def test_install_root_refuses_a_missing_parent(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    with pytest.raises(InstallError, match="parent directory does not exist"):
        resolve_install_root(str(tmp_path / "absent" / "root"), bundle_root=bundle)


def test_install_root_accepts_an_absolute_path_and_expands_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert (
        resolve_install_root(str(tmp_path / "root"), bundle_root=bundle)
        == (tmp_path / "root").resolve()
    )
    assert resolve_install_root("~/tools", bundle_root=bundle) == (home / "tools").resolve()


def test_prompt_reprompts_after_a_rejected_answer_and_creates_nothing_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    good = tmp_path / "installed"
    good.mkdir()
    answers = iter([_SECRET_SHAPED, str(good)])
    stream = io.StringIO()

    chosen = prompt_install_root(
        tmp_path / "default",
        bundle_root=bundle,
        input_fn=lambda _prompt: next(answers),
        stream=stream,
    )

    assert chosen == good.resolve()
    assert not (workdir / _SECRET_SHAPED).exists()
    assert "rejected" in stream.getvalue()
    assert _SECRET_SHAPED not in stream.getvalue()


def test_prompt_requires_confirmation_before_creating_a_new_root(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    fresh = tmp_path / "brand-new"
    existing = tmp_path / "already-there"
    existing.mkdir()
    answers = iter([str(fresh), "n", str(existing)])

    chosen = prompt_install_root(
        tmp_path / "default",
        bundle_root=bundle,
        input_fn=lambda _prompt: next(answers),
        stream=io.StringIO(),
    )

    assert chosen == existing.resolve()
    assert not fresh.exists()


def test_prompt_gives_up_rather_than_installing_somewhere_unvalidated(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    answers = iter([_SECRET_SHAPED, "also-not-a-path", "still/not/absolute"])

    with pytest.raises(InstallError, match="no usable install root after 3 attempts"):
        prompt_install_root(
            tmp_path / "default",
            bundle_root=bundle,
            input_fn=lambda _prompt: next(answers),
            stream=io.StringIO(),
        )


def test_installed_support_preserves_docs_without_a_second_source_copy(tmp_path: Path) -> None:
    bundle = tmp_path / "sample-tool-release-1"
    bundle.mkdir()
    (bundle / "launch-docs.py").write_text("# launcher\n", encoding="utf-8")
    (bundle / "docs-index.html").write_text("docs\n", encoding="utf-8")
    (bundle / "pack-manifest.json").write_text("{}\n", encoding="utf-8")
    (bundle / "deploy").mkdir()
    (bundle / "deploy" / "__init__.py").write_text("", encoding="utf-8")
    (bundle / "deploy" / "checks.py").write_text("# checks\n", encoding="utf-8")
    install_root = tmp_path / "installed"
    release = install_root / "releases" / "release-1"
    release.mkdir(parents=True)

    support = install_support(bundle, install_root, release, "release-1")

    assert (support / "launch-docs.py").read_text(encoding="utf-8") == "# launcher\n"
    assert (support / "docs-index.html").is_file()
    assert (support / "deploy" / "checks.py").is_file()
    assert (support / "source").is_symlink()
    assert (support / "source").resolve() == release.resolve()


def test_cleanup_removes_only_exact_transfer_artifacts(tmp_path: Path) -> None:
    bundle = tmp_path / "sample-tool-release-1"
    bundle.mkdir()
    (bundle / "pack-manifest.json").write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "sample-tool-release-1.tar.gz"
    checksum = tmp_path / "sample-tool-release-1.tar.gz.sha256"
    archive.write_bytes(b"archive")
    checksum.write_text("checksum\n", encoding="utf-8")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep\n", encoding="utf-8")
    installed_release = tmp_path / "installed" / "releases" / "release-1"
    installed_release.mkdir(parents=True)
    manifest = {
        "bundle_id": "release-1",
        "tool": {"name": "sample-tool"},
    }

    assert bundle_cleanup_targets(bundle, manifest) == [archive, checksum, bundle]
    removed = cleanup_bundle_artifacts(bundle, manifest)

    assert removed == [archive, checksum, bundle]
    assert not archive.exists()
    assert not checksum.exists()
    assert not bundle.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep\n"
    assert installed_release.is_dir()


def test_cleanup_refuses_a_renamed_bundle_directory(tmp_path: Path) -> None:
    bundle = tmp_path / "renamed"
    bundle.mkdir()
    manifest = {
        "bundle_id": "release-1",
        "tool": {"name": "sample-tool"},
    }

    with pytest.raises(InstallError, match="cleanup refused"):
        cleanup_bundle_artifacts(bundle, manifest)

    assert bundle.is_dir()


def test_install_root_owned_by_another_tool_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    mark_install_root(root, "another-tool")

    with pytest.raises(InstallError, match="belongs to another tool"):
        install_root_needs_adoption(root, "sample-tool")


def test_render_service_unit_reads_host_config_and_secret_file(tmp_path: Path) -> None:
    service = {
        "description": "Sample Tool",
        "entrypoint": "sample-tool",
        "args": ["serve"],
    }
    unit = render_service_unit(
        service,
        home=tmp_path,
        current=tmp_path / ".local/opt/sample/current",
        environment_file=tmp_path / ".config/sample/sample.env",
        secrets_file=tmp_path / ".config/sample/secrets.env",
    )

    assert f"EnvironmentFile=-{tmp_path}/.config/sample/sample.env" in unit
    assert f"EnvironmentFile=-{tmp_path}/.config/sample/secrets.env" in unit
    assert 'Environment="SAMPLE_REPO=' not in unit
    assert f"WorkingDirectory={tmp_path}/.local/opt/sample/current" in unit
    assert 'ExecStart="' in unit
    assert 'sample-tool" "serve' in unit


def test_rendered_service_unit_passes_systemd_parser_when_available(tmp_path: Path) -> None:
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is None:
        pytest.skip("systemd-analyze is not installed")
    executable = tmp_path / ".local" / "bin" / "sample-tool"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    current = tmp_path / "src" / "sample" / "current"
    current.mkdir(parents=True)
    secrets = tmp_path / ".config" / "sample" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("SAMPLE_API_KEY=test\n", encoding="utf-8")
    unit = render_service_unit(
        {
            "description": "Sample Tool",
            "entrypoint": "sample-tool",
            "args": ["serve"],
        },
        home=tmp_path,
        current=current,
        environment_file=tmp_path / ".config/sample/sample.env",
        secrets_file=secrets,
    )
    unit_path = tmp_path / "sample-tool.service"
    unit_path.write_text(unit, encoding="utf-8")

    completed = subprocess.run(
        [systemd_analyze, "--user", "verify", str(unit_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_differing_systemd_unit_is_backed_up_before_replacement(tmp_path: Path) -> None:
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit = unit_dir / "sample-tool.service"
    unit.write_text("old unit\n", encoding="utf-8")
    service = {
        "unit": "sample-tool.service",
        "description": "Sample Tool",
        "entrypoint": "sample-tool",
        "args": ["serve"],
    }

    units = _write_units(
        [service],
        home=tmp_path,
        current=tmp_path / "src/sample/current",
        environment_file=tmp_path / ".config/sample/sample.env",
        secrets_file=tmp_path / ".config/sample/secrets.env",
    )

    backups = list(unit_dir.glob("sample-tool.service.bak-*"))
    assert units == ["sample-tool.service"]
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old unit\n"
    assert unit.read_text(encoding="utf-8") != "old unit\n"


def test_host_environment_update_preserves_unmanaged_lines_and_backs_up(tmp_path: Path) -> None:
    path = tmp_path / ".config" / "sample" / "sample.env"
    path.parent.mkdir(parents=True)
    original = "# operator setting\nUNMANAGED=yes\nSAMPLE_REPO=/old\n"
    path.write_text(original, encoding="utf-8")

    backup = ensure_environment_file(
        path,
        {
            "SAMPLE_REPO": "/srv/sample/current",
            "SAMPLE_DATA_DIR": "/srv/sample/data",
        },
    )

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == original
    assert path.read_text(encoding="utf-8") == (
        "# operator setting\n"
        "UNMANAGED=yes\n"
        "SAMPLE_REPO=/srv/sample/current\n"
        "\n"
        "SAMPLE_DATA_DIR=/srv/sample/data\n"
    )


def test_checksum_uses_sha256sum_format(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = build_bundle(repo, _spec(), tmp_path / "output")
    digest, filename = result.checksum.read_text(encoding="utf-8").strip().split(maxsplit=1)

    assert len(digest) == 64
    assert filename == result.archive.name
    int(digest, 16)


def test_extracted_installer_dry_run_never_prompts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "deploy/install.py").write_text(
        (ROOT / "deploy/install.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = build_bundle(repo, _spec(), tmp_path / "output")
    extracted = tmp_path / "extracted"
    with tarfile.open(result.archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")

    completed = subprocess.run(
        [sys.executable, str(extracted / result.bundle_root / "install.py"), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    assert "New release:" in completed.stdout


def test_noninteractive_installer_refuses_an_unmanaged_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "deploy/install.py").write_text(
        (ROOT / "deploy/install.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = build_bundle(repo, _spec(), tmp_path / "output")
    extracted = tmp_path / "extracted"
    with tarfile.open(result.archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    install_root = tmp_path / "existing"
    install_root.mkdir()
    existing = install_root / "keep.txt"
    existing.write_text("keep me\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(extracted / result.bundle_root / "install.py"),
            "--yes",
            "--skip-service",
            "--install-root",
            str(install_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    assert completed.returncode == 1
    assert "contains files from outside the pack installer" in completed.stderr
    assert existing.read_text(encoding="utf-8") == "keep me\n"
    assert not (install_root / ".tool-pack-root.json").exists()


def test_extracted_docs_launcher_validates_payload(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "deploy/docs.py").write_text(
        (ROOT / "deploy/docs.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = build_bundle(repo, _spec(), tmp_path / "output")
    extracted = tmp_path / "extracted"
    with tarfile.open(result.archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")

    completed = subprocess.run(
        [sys.executable, str(extracted / result.bundle_root / "launch-docs.py"), "--check"],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    assert "Documentation ready:" in completed.stdout


def test_component_panel_proves_release_command_config_and_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    install_root = home / "src" / "sample-tool"
    release = install_root / "releases" / "release-1"
    release.mkdir(parents=True)
    install_root.mkdir(parents=True, exist_ok=True)
    (install_root / "current").symlink_to(release)
    executable = home / ".local" / "bin" / "sample-tool"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    secrets = home / ".config" / "sample-tool" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("SAMPLE_API_KEY=configured\n", encoding="utf-8")
    environment_file = home / ".config" / "sample-tool" / "sample-tool.env"
    environment_file.write_text(
        f"SAMPLE_REPO={install_root / 'current'}\n"
        f"SAMPLE_DATA_DIR={home / '.local/share/sample-tool'}\n",
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle"
    (bundle / "source").mkdir(parents=True)
    (bundle / "docs-index.html").write_text("docs\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    manifest = {
        "tool": {"name": "sample-tool"},
        "install": {
            "default_root": "~/src/sample-tool",
            "repo_environment": "SAMPLE_REPO",
            "environment_file": "~/.config/sample-tool/sample-tool.env",
            "environment": {"SAMPLE_DATA_DIR": "~/.local/share/sample-tool"},
            "secrets_file": "~/.config/sample-tool/secrets.env",
            "required_secrets": ["SAMPLE_API_KEY"],
        },
        "services": [
            {
                "unit": "sample-tool.service",
                "entrypoint": "sample-tool",
                "health_url": "http://127.0.0.1:9000/health",
                "health_service": "sample-tool",
            }
        ],
        "capability": {"token_file": "~/.local/share/sample-tool/token"},
    }

    checks = run_component_checks(manifest, bundle, include_services=False)

    by_key = {check.key: check for check in checks}
    assert by_key["release"].state == "pass"
    assert by_key["command"].state == "pass"
    assert by_key["host_configuration"].state == "pass"
    assert by_key["configuration"].state == "pass"
    assert by_key["service"].state == "skip"
    assert by_key["health"].state == "skip"
    assert by_key["capability"].state == "skip"
    assert by_key["documentation"].state == "pass"
    assert required_checks_pass(checks)


def test_docs_index_contains_toggle_driven_component_front_panel(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = build_bundle(repo, _spec(), tmp_path / "output")

    with tarfile.open(result.archive, "r:gz") as bundle:
        extracted = bundle.extractfile(f"{result.bundle_root}/docs-index.html")
        assert extracted is not None
        index = extracted.read().decode()
    assert "Installation front panel" in index
    assert "Opening this panel runs local" in index
    assert "fetch('/__pack/checks'" in index
    assert "panel.addEventListener('toggle'" in index


def test_existing_environment_value_reads_a_quoted_setting(tmp_path: Path) -> None:
    env_file = tmp_path / "alexandria.env"
    env_file.write_text(
        "# comment\nALEXANDRIA_DATA_DIR=/somewhere\nALEXANDRIA_REPO='/home/dhk/corpus'\n",
        encoding="utf-8",
    )

    assert existing_environment_value(env_file, "ALEXANDRIA_REPO") == "/home/dhk/corpus"
    assert existing_environment_value(env_file, "NOT_SET") is None
    assert existing_environment_value(tmp_path / "absent.env", "ALEXANDRIA_REPO") is None


def test_external_repo_prefers_the_explicit_flag(tmp_path: Path) -> None:
    resolved = _resolve_external_repo(tmp_path / "flagged", "/ignored/existing")

    assert resolved == (tmp_path / "flagged").resolve()


def test_external_repo_falls_back_to_what_the_operator_already_configured() -> None:
    resolved = _resolve_external_repo(None, "~/corpus")

    assert resolved == (Path.home() / "corpus").resolve()


def test_external_repo_refuses_to_invent_a_default() -> None:
    # The caller turns None into a hard error. Returning any path here would
    # reintroduce the bug this exists to prevent: a healthy server quietly
    # serving a frozen copy of the corpus from inside its own release.
    assert _resolve_external_repo(None, None) is None
    assert _resolve_external_repo(None, "") is None


def test_pack_toml_keeps_the_corpus_outside_the_install_root() -> None:
    # Regression guard: this pack's data repository is dhk/alexandria, a
    # different repo from the one being installed.
    config = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "deploy" / "pack.toml").read_text(encoding="utf-8")
    )

    assert config["pack"]["repo_is_install_root"] is False
