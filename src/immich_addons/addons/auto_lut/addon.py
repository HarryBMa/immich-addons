"""auto-lut — apply a .cube LUT to new assets and stack the graded copy with the original.

Phase 2 ships the configuration surface only; :meth:`AutoLut.run` lands in Phase 3 (PLAN.md §6.1).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from immich_addons.addons.base import Addon, AddonConfig
from immich_addons.core.jobs import JobContext


class AutoLutConfig(AddonConfig):
    lut: str = Field(
        default="",
        title="LUT",
        description="A .cube file in /data/luts.",
        json_schema_extra={"x-picker": "luts"},
    )
    intensity: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        title="Intensity",
        description="Blend of the graded image over the original. 1.0 is the LUT at full strength.",
    )
    album_ids: list[str] = Field(
        default_factory=list,
        title="Limit to albums",
        description="Leave empty to process every new asset.",
    )
    camera_models: list[str] = Field(
        default_factory=list,
        title="Limit to camera models",
        description="Matched against the asset's EXIF model, case-insensitively.",
    )
    extensions: list[str] = Field(
        default_factory=lambda: ["jpg", "jpeg", "heic"],
        title="File extensions",
        description="RAW is skipped in v1 regardless of what is listed here.",
    )
    process_videos: bool = Field(
        default=False,
        title="Process videos",
        description="Videos are re-encoded, which is slow. Off by default.",
    )
    stack_original: bool = Field(
        default=True,
        title="Stack with the original",
        description="Keeps the pair together in Immich instead of showing two separate assets.",
    )


class AutoLut(Addon):
    id: ClassVar[str] = "auto-lut"
    name: ClassVar[str] = "Auto-LUT"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Applies a chosen .cube LUT to new photos/videos and stacks the graded copy "
        "with the original."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("webhook", "poll")
    config_model: ClassVar[type[AddonConfig]] = AutoLutConfig

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        raise NotImplementedError("auto-lut lands in Phase 3 (PLAN.md §6.1)")

    def on_event(self, event: dict[str, Any], config: AddonConfig) -> list[dict[str, Any]]:
        raise NotImplementedError("auto-lut lands in Phase 3 (PLAN.md §6.1)")
