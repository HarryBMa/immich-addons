"""trip-best-picks — the best *and most varied* photos from a trip, into an album.

Phase 2 ships the configuration surface only; the pipeline lands in Phase 4 (PLAN.md §6.2).
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from immich_addons.addons.base import Addon, AddonConfig
from immich_addons.core.jobs import JobContext


class TripBestPicksConfig(AddonConfig):
    source: Literal["album", "date_range", "trip"] = Field(
        default="trip",
        title="Source",
        description="Where the candidate photos come from.",
    )
    album_id: str = Field(default="", title="Album", json_schema_extra={"x-picker": "albums"})
    date_from: date | None = Field(default=None, title="From")
    date_to: date | None = Field(default=None, title="To")
    n_picks: int = Field(default=24, ge=1, le=500, title="Number of picks")
    people_boost: list[str] = Field(
        default_factory=list,
        title="Favour these people",
        description="Photos containing them score higher.",
        json_schema_extra={"x-picker": "people"},
    )
    embedding_source: Literal["db", "local"] = Field(
        default="db",
        title="Embeddings from",
        description=(
            "db reads Immich's CLIP vectors (fast, needs the read-only Postgres role); "
            "local computes them on CPU (slower, no database access)."
        ),
    )
    diversity: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        title="Quality vs variety",
        description="MMR lambda. 1.0 picks purely on quality; lower values spread the selection.",
    )

    @model_validator(mode="after")
    def _check_source_fields(self) -> TripBestPicksConfig:
        if self.source == "album" and not self.album_id:
            raise ValueError("source 'album' needs an album_id")
        if self.source == "date_range" and not (self.date_from and self.date_to):
            raise ValueError("source 'date_range' needs both date_from and date_to")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        return self


class TripBestPicks(Addon):
    id: ClassVar[str] = "trip-best-picks"
    name: ClassVar[str] = "Trip Best Picks"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Selects the best photos from a trip while keeping the selection varied, "
        "and collects them in an album."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("manual",)
    config_model: ClassVar[type[AddonConfig]] = TripBestPicksConfig

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        raise NotImplementedError("trip-best-picks lands in Phase 4 (PLAN.md §6.2)")
