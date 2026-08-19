"""year-highlights — cut a year of video into a music-backed highlight film.

Phase 2 ships the configuration surface only; scene detection and assembly land in Phase 6
(PLAN.md §6.4).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from immich_addons.addons.base import Addon, AddonConfig
from immich_addons.core.jobs import JobContext


class YearHighlightsConfig(AddonConfig):
    year: int = Field(default=2026, ge=1900, le=2200, title="Year")
    target_length_s: int = Field(default=180, ge=15, le=1800, title="Target length (seconds)")
    people_filter: list[str] = Field(
        default_factory=list,
        title="Only events featuring",
        json_schema_extra={"x-picker": "people"},
    )
    music_file: str = Field(
        default="",
        title="Music",
        description=(
            "A file in /data/music. Use your own or licensed audio only. "
            "Leave empty to keep the clips' own sound."
        ),
        json_schema_extra={"x-picker": "music"},
    )
    include_photos: bool = Field(
        default=True,
        title="Mix in photos",
        description="Stills get a 2 second Ken Burns move.",
    )
    month_titles: bool = Field(default=True, title="Month title cards")


class YearHighlights(Addon):
    id: ClassVar[str] = "year-highlights"
    name: ClassVar[str] = "Year Highlights"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Finds the best moments across a year of video and cuts them into one film, "
        "with music and month titles."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("manual", "schedule")
    config_model: ClassVar[type[AddonConfig]] = YearHighlightsConfig

    #: A run is an overnight job that caches per-scene work, so an interrupted job is requeued
    #: rather than failed (see JobQueue.resumable_addons).
    resumable: ClassVar[bool] = True

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        raise NotImplementedError("year-highlights lands in Phase 6 (PLAN.md §6.4)")
