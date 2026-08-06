from __future__ import annotations

from pathlib import Path

import pytest

from immich_addons.core.config import MissingSettingError, Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_parses_into_settings() -> None:
    """`.env.example` must be copyable to `.env` and load cleanly.

    Guards against inline-comment and type mistakes in the template — a broken example is a
    broken first run for anyone installing this.
    """
    settings = Settings(_env_file=REPO_ROOT / ".env.example")

    assert settings.immich_url == "http://immich-server:2283"
    assert settings.hub_port == 8484
    assert settings.immich_db_port == 5432
    assert settings.immich_db_user == "addons_ro"
    assert settings.data_dir == Path("/data")
    assert settings.trigger_mode == "webhook"
    assert settings.dry_run_default is True


def test_env_example_covers_every_setting() -> None:
    """Every field has a line in the template, so nobody has to read the source to configure it."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {
        line.split("=", 1)[0].strip().lower()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert set(Settings.model_fields) - documented == set()


def test_dry_run_defaults_to_true_in_the_template() -> None:
    """CLAUDE.md: every addon is dry-run by default until explicitly disabled."""
    assert Settings(_env_file=REPO_ROOT / ".env.example").dry_run_default is True


def test_require_names_the_missing_settings() -> None:
    settings = Settings(immich_api_key="", hub_password="", _env_file=None)
    with pytest.raises(MissingSettingError) as excinfo:
        settings.require("immich_api_key", "hub_password")
    message = str(excinfo.value)
    assert "immich_api_key" in message
    assert "hub_password" in message


def test_urls_lose_their_trailing_slash() -> None:
    settings = Settings(immich_url="http://immich.test:2283/", _env_file=None)
    assert settings.immich_url == "http://immich.test:2283"


def test_data_dir_layout(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, _env_file=None)
    assert settings.luts_dir == tmp_path / "luts"
    assert settings.jobs_db_path == tmp_path / "jobs.sqlite"
    settings.ensure_data_dirs()
    assert settings.cache_dir.is_dir()
    assert settings.output_dir.is_dir()


def test_db_dsn_refuses_to_build_without_credentials() -> None:
    with pytest.raises(MissingSettingError):
        _ = Settings(immich_db_host="", _env_file=None).db_dsn


def test_db_dsn_uses_the_read_only_role() -> None:
    settings = Settings(immich_db_host="pg.test", immich_db_password="secret", _env_file=None)
    assert "user=addons_ro" in settings.db_dsn
    assert "host=pg.test" in settings.db_dsn
