from pathlib import Path

import pytest

from alexandria.infrastructure.config import (
    DEFAULT_HOST_ENV_FILE,
    ENV_DATA_DIR,
    ENV_REPO_ROOT,
    HostEnvironmentError,
    RepoNotFoundError,
    load_config,
)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "DESIGN.md").write_text("design", encoding="utf-8")
    (repo / "AGENTS.md").write_text("agents", encoding="utf-8")
    return repo


def test_repo_root_env_override_wins(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = load_config(
        {ENV_REPO_ROOT: str(repo)}, cwd=tmp_path, host_env_file=tmp_path / "missing.env"
    )
    assert config.repo_root == repo
    assert config.repo_root_source == f"{ENV_REPO_ROOT} environment variable"


def test_repo_root_detected_from_cwd(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    nested = repo / "research" / "some-slug"
    nested.mkdir(parents=True)
    config = load_config({}, cwd=nested, host_env_file=tmp_path / "missing.env")
    assert config.repo_root == repo


def test_repo_root_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(RepoNotFoundError):
        load_config({}, cwd=tmp_path, host_env_file=tmp_path / "missing.env")


def test_data_dir_env_override_wins(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = load_config(
        {ENV_DATA_DIR: str(tmp_path / "state"), ENV_REPO_ROOT: str(repo)},
        cwd=tmp_path,
        host_env_file=tmp_path / "missing.env",
    )
    assert config.data_dir == tmp_path / "state"
    assert config.data_dir_source == f"{ENV_DATA_DIR} environment variable"


def test_research_dir_derived_from_repo_root(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = load_config(
        {ENV_REPO_ROOT: str(repo)}, cwd=tmp_path, host_env_file=tmp_path / "missing.env"
    )
    assert config.research_dir == repo / "research"


def test_canonical_host_file_resolves_repo_and_data_from_an_unrelated_cwd(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    host_file = tmp_path / ".config" / "alexandria" / "alexandria.env"
    host_file.parent.mkdir(parents=True)
    host_file.write_text(
        f"{ENV_REPO_ROOT}='{repo}'\n{ENV_DATA_DIR}={tmp_path / 'state'}\n",
        encoding="utf-8",
    )

    config = load_config({}, cwd=tmp_path / "elsewhere", host_env_file=host_file)

    assert config.repo_root == repo
    assert config.data_dir == tmp_path / "state"
    assert config.repo_root_source == f"{ENV_REPO_ROOT} in {host_file}"


def test_process_environment_precedes_host_file(tmp_path: Path) -> None:
    file_repo = _make_repo(tmp_path / "file")
    process_repo = _make_repo(tmp_path / "process")
    host_file = tmp_path / "alexandria.env"
    host_file.write_text(f"{ENV_REPO_ROOT}={file_repo}\n", encoding="utf-8")

    config = load_config({ENV_REPO_ROOT: str(process_repo)}, cwd=tmp_path, host_env_file=host_file)

    assert config.repo_root == process_repo
    assert config.repo_root_source == f"{ENV_REPO_ROOT} environment variable"


def test_malformed_host_file_reports_file_and_line(tmp_path: Path) -> None:
    host_file = tmp_path / "alexandria.env"
    host_file.write_text(f"{ENV_REPO_ROOT}\n", encoding="utf-8")

    with pytest.raises(HostEnvironmentError, match=r"alexandria\.env:1: expected NAME=value"):
        load_config({}, cwd=tmp_path, host_env_file=host_file)


def test_host_file_is_parsed_as_data_and_unknown_entries_are_ignored(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sentinel = tmp_path / "must-not-exist"
    host_file = tmp_path / "alexandria.env"
    host_file.write_text(
        f"IGNORED=$(touch {sentinel})\n{ENV_REPO_ROOT}={repo}\n",
        encoding="utf-8",
    )

    assert load_config({}, cwd=tmp_path, host_env_file=host_file).repo_root == repo
    assert not sentinel.exists()


def test_default_host_file_is_canonical_nested_path() -> None:
    assert str(DEFAULT_HOST_ENV_FILE) == "~/.config/alexandria/alexandria.env"
