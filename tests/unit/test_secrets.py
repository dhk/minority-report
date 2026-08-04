from pathlib import Path

import pytest

from alexandria.infrastructure.secrets import SecretNotFoundError, openrouter_api_key


def test_openrouter_key_prefers_environment(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("OPENROUTER_API_KEY=file-key\n", encoding="utf-8")
    assert (
        openrouter_api_key(
            {"OPENROUTER_API_KEY": "env-key", "ALEXANDRIA_SECRETS_FILE": str(secrets)}
        )
        == "env-key"
    )


def test_openrouter_key_reads_configured_file(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("# local only\nOPENROUTER_API_KEY='file-key'\n", encoding="utf-8")
    assert openrouter_api_key({"ALEXANDRIA_SECRETS_FILE": str(secrets)}) == "file-key"


def test_openrouter_key_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SecretNotFoundError):
        openrouter_api_key({"ALEXANDRIA_SECRETS_FILE": str(tmp_path / "missing.env")})


def test_openrouter_key_uses_secrets_pointer_from_canonical_host_file(tmp_path: Path) -> None:
    secrets = tmp_path / "keys.env"
    secrets.write_text("OPENROUTER_API_KEY=file-key\n", encoding="utf-8")
    host_file = tmp_path / "alexandria.env"
    host_file.write_text(f"ALEXANDRIA_SECRETS_FILE={secrets}\n", encoding="utf-8")

    assert openrouter_api_key({}, host_env_file=host_file) == "file-key"
