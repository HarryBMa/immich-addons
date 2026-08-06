"""Settings, loaded from the environment / ``.env`` (see ``.env.example``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TriggerMode = Literal["webhook", "poll"]


class MissingSettingError(RuntimeError):
    """Raised when a setting needed for the operation at hand is empty."""


class Settings(BaseSettings):
    """Every key in ``.env.example``, in the same order.

    Nothing here is required at import time: the hub must be able to boot with a half-filled
    ``.env`` and *say so* in the UI rather than crash. Call :meth:`require` before doing work that
    actually needs a value.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    immich_url: str = "http://immich-server:2283"
    immich_api_key: str = ""
    immich_version: str = ""

    immich_db_host: str = ""
    immich_db_port: int = 5432
    immich_db_name: str = "immich"
    immich_db_user: str = "addons_ro"
    immich_db_password: str = ""

    hub_port: int = 8484
    hub_password: str = ""
    embed_origin: str = ""

    data_dir: Path = Path("/data")
    trigger_mode: TriggerMode = "webhook"
    ollama_url: str = ""
    dry_run_default: bool = True

    @field_validator("immich_url", "ollama_url", "embed_origin", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    def require(self, *names: str) -> None:
        """Raise unless every named setting is non-empty.

        Used at the top of anything that talks to Immich, so a missing API key fails with the
        setting's name instead of a 401 from three layers down.
        """
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise MissingSettingError(
                "missing required setting(s): " + ", ".join(sorted(missing)) + " (see .env.example)"
            )

    # --- paths under DATA_DIR -------------------------------------------------------------

    @property
    def luts_dir(self) -> Path:
        return self.data_dir / "luts"

    @property
    def music_dir(self) -> Path:
        return self.data_dir / "music"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite"

    def ensure_data_dirs(self) -> None:
        for p in (self.luts_dir, self.music_dir, self.output_dir, self.cache_dir):
            p.mkdir(parents=True, exist_ok=True)

    # --- derived ---------------------------------------------------------------------------

    @property
    def db_dsn(self) -> str:
        """libpq connection string for the **read-only** ``addons_ro`` role."""
        self.require("immich_db_host", "immich_db_password")
        return (
            f"host={self.immich_db_host} port={self.immich_db_port} "
            f"dbname={self.immich_db_name} user={self.immich_db_user} "
            f"password={self.immich_db_password}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached; call ``get_settings.cache_clear()`` in tests."""
    return Settings()
