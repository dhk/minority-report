from pathlib import Path

from example_service.infrastructure.config import ENV_DATA_DIR, load_config


def test_env_override_wins() -> None:
    config = load_config({ENV_DATA_DIR: "/tmp/example-service-data"})
    assert config.data_dir == Path("/tmp/example-service-data")
    assert config.data_dir_source == f"{ENV_DATA_DIR} environment variable"


def test_default_uses_platform_dir() -> None:
    config = load_config({})
    assert config.data_dir_source == "platform user data directory"
    assert config.data_dir.name == "example_service" or "example_service" in str(config.data_dir)
