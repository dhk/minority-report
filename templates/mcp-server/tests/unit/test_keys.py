from example_service.infrastructure.keys import (
    KNOWN_KEYS,
    ensure_env,
    read_host_keys,
    resolve_key_sources,
)


def test_read_host_keys_parses_known_names_only(tmp_path, monkeypatch) -> None:
    home = tmp_path
    (home / ".config").mkdir()
    (home / ".config" / "example_service.env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-abc\nSOME_UNKNOWN=nope\n", encoding="utf-8"
    )
    values = read_host_keys(home=home)
    assert values == {"ANTHROPIC_API_KEY": "sk-ant-abc"}


def test_ensure_env_does_not_override_existing(tmp_path, monkeypatch) -> None:
    home = tmp_path
    (home / ".config").mkdir()
    (home / ".config" / "example_service.env").write_text(
        "ANTHROPIC_API_KEY=from-file\n", encoding="utf-8"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    ensure_env(home=home)
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "from-env"


def test_resolve_key_sources_flags_disagreement(tmp_path) -> None:
    home = tmp_path
    (home / ".config").mkdir()
    (home / ".config" / "example_service.env").write_text(
        "ANTHROPIC_API_KEY=from-host\n", encoding="utf-8"
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "keys.env").write_text("ANTHROPIC_API_KEY=from-workspace\n", encoding="utf-8")
    sources = resolve_key_sources({}, data_dir=data_dir, home=home)
    anthropic = next(s for s in sources if s.short_name == "anthropic")
    assert anthropic.winning_source.startswith("host file")
    assert any("workspace" in s for s in anthropic.shadowed_by)


def test_known_keys_nonempty() -> None:
    assert "anthropic" in KNOWN_KEYS
