"""zine-maker — lay out a printable zine as PDF.

Phase 2 ships the configuration surface only; imposition and rendering land in Phase 5
(PLAN.md §6.3).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from immich_addons.addons.base import Addon, AddonConfig
from immich_addons.core.jobs import JobContext


class ZineMakerConfig(AddonConfig):
    title: str = Field(default="", title="Title", description="Printed on the cover.")
    topic: str = Field(
        default="",
        title="Topic",
        description='Free-text search, e.g. "kids at the beach". Ignored when an album is chosen.',
    )
    album_id: str = Field(default="", title="Album", json_schema_extra={"x-picker": "albums"})
    pages: Literal[8, 16] = Field(default=8, title="Pages")
    page_orientation: Literal["portrait", "landscape"] = Field(
        default="portrait", title="Page orientation"
    )
    layout: Literal["mini8", "booklet"] = Field(
        default="mini8",
        title="Format",
        description=(
            "mini8 is one A4 sheet folded into an 8-page zine; "
            "booklet is A5 saddle-stitch from duplex A4."
        ),
    )
    sequence: Literal["chronological", "arc"] = Field(
        default="chronological",
        title="Sequencing",
        description="arc groups similar scenes together instead of following the clock.",
    )
    captions: Literal["none", "exif", "ollama"] = Field(
        default="exif",
        title="Captions",
        description="ollama needs OLLAMA_URL set; it falls back to exif when unreachable.",
    )
    bleed_marks: bool = Field(default=True, title="Print bleed and cut marks")

    @model_validator(mode="after")
    def _check_source(self) -> ZineMakerConfig:
        if not self.topic and not self.album_id:
            raise ValueError("give either a topic or an album")
        if self.layout == "mini8" and self.pages != 8:
            raise ValueError("the mini8 format is exactly 8 pages")
        return self


class ZineMaker(Addon):
    id: ClassVar[str] = "zine-maker"
    name: ClassVar[str] = "Zine Maker"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Lays out a printable zine from a search or an album: a folded single sheet "
        "or a saddle-stitch booklet, as print-ready PDF."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("manual",)
    config_model: ClassVar[type[AddonConfig]] = ZineMakerConfig

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        raise NotImplementedError("zine-maker lands in Phase 5 (PLAN.md §6.3)")
