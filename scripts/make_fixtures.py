#!/usr/bin/env python
"""Generate the development fixture set.

PLAN.md asks for ~30 CC0 photos and 3 short clips. Rather than vendoring stock media of uncertain
provenance into the repo, this script *synthesises* a corpus with the properties the acceptance
tests actually depend on:

* **bursts** — five near-identical frames, one of them sharper, so near_dup_groups has something
  to collapse and mmr_select has an obvious right answer (Phase 4);
* **mixed orientations and aspect ratios** — portrait, landscape and square, so the zine's
  aspect-aware template assignment is exercised (Phase 5);
* **distinct scenes** — different palettes and compositions, so "picks span the scenes" is
  measurable;
* **a spread of capture dates across twelve months**, written into EXIF, so year-highlights has
  every month to cover (Phase 6);
* **three short clips** with motion, for scene detection.

    uv run python scripts/make_fixtures.py           # writes fixtures/
    uv run python scripts/make_fixtures.py --clean   # regenerate from scratch

Photos need Pillow (a dev dependency). Clips need ffmpeg on PATH; without it the script writes the
photos, says so, and exits 0.
"""

from __future__ import annotations

import argparse
import colorsys
import math
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"
PHOTOS = FIXTURES / "photos"
CLIPS = FIXTURES / "clips"

SEED = 20260806  # deterministic: the same fixtures on every machine


@dataclass(frozen=True)
class Scene:
    name: str
    hue: float
    orientation: str  # portrait | landscape | square
    month: int


SCENES: tuple[Scene, ...] = (
    Scene("winter-harbour", 0.58, "landscape", 1),
    Scene("snow-walk", 0.55, "portrait", 2),
    Scene("first-thaw", 0.30, "landscape", 3),
    Scene("garden", 0.28, "square", 4),
    Scene("bike-ride", 0.13, "landscape", 5),
    Scene("midsummer", 0.16, "portrait", 6),
    Scene("beach", 0.10, "landscape", 7),
    Scene("forest", 0.35, "portrait", 8),
    Scene("harvest", 0.08, "landscape", 9),
    Scene("autumn-park", 0.05, "square", 10),
    Scene("lantern-night", 0.72, "portrait", 11),
    Scene("december-lights", 0.95, "landscape", 12),
)

SIZES = {"landscape": (1600, 1067), "portrait": (1067, 1600), "square": (1280, 1280)}


def _rgb(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, saturation, value)
    return int(r * 255), int(g * 255), int(b * 255)


def _draw_scene(scene: Scene, *, variant: int, sharp: bool, rng: random.Random):  # noqa: ANN202
    from PIL import Image, ImageDraw, ImageFilter

    width, height = SIZES[scene.orientation]
    image = Image.new("RGB", (width, height), _rgb(scene.hue, 0.25, 0.92))
    draw = ImageDraw.Draw(image)

    # A horizon band and a few shapes: enough structure that sharpness and CLIP embeddings differ
    # between scenes but stay similar within one.
    horizon = int(height * (0.55 + 0.02 * math.sin(variant)))
    draw.rectangle([0, horizon, width, height], fill=_rgb(scene.hue + 0.04, 0.45, 0.55))
    for i in range(6):
        cx = int(width * (0.1 + 0.15 * i)) + rng.randint(-12, 12) * variant
        radius = int(min(width, height) * (0.05 + 0.02 * ((i + variant) % 3)))
        cy = horizon - radius - rng.randint(0, 60)
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            fill=_rgb(scene.hue + 0.1 * i, 0.6, 0.85),
        )
    draw.rectangle(
        [int(width * 0.62), horizon - int(height * 0.3), int(width * 0.78), horizon],
        fill=_rgb(scene.hue + 0.5, 0.3, 0.35),
    )

    if not sharp:
        # Burst members that are not the keeper: slightly soft, which is exactly what the
        # sharpness term is meant to notice.
        image = image.filter(ImageFilter.GaussianBlur(radius=1.6))
    return image


def _exif_bytes(taken: datetime, model: str):  # noqa: ANN202
    from PIL import Image

    exif = Image.Exif()
    exif[0x010F] = "immich-addons"  # Make
    exif[0x0110] = model  # Model
    exif[0x0132] = taken.strftime("%Y:%m:%d %H:%M:%S")  # DateTime
    exif[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
    return exif


def make_photos() -> list[Path]:
    rng = random.Random(SEED)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for scene in SCENES:
        base = datetime(2026, scene.month, 12, 14, 30, tzinfo=UTC)
        # Two singles per scene, plus one five-frame burst in three of them.
        for i in range(2):
            taken = base + timedelta(hours=i)
            image = _draw_scene(scene, variant=i, sharp=True, rng=rng)
            path = PHOTOS / f"{scene.month:02d}_{scene.name}_{i}.jpg"
            image.save(path, quality=92, exif=_exif_bytes(taken, "ILCE-6400"))
            written.append(path)

        if scene.month in {5, 7, 11}:
            for frame in range(5):
                taken = base + timedelta(minutes=30, seconds=frame)
                # Frame 2 is the keeper: only it is sharp.
                image = _draw_scene(scene, variant=10 + frame, sharp=frame == 2, rng=rng)
                path = PHOTOS / f"{scene.month:02d}_{scene.name}_burst{frame}.jpg"
                image.save(path, quality=92, exif=_exif_bytes(taken, "ILCE-6400"))
                written.append(path)

    return written


def make_clips() -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not on PATH — skipping the video fixtures", file=sys.stderr)
        return []

    CLIPS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    recipes = [
        ("05_bike-ride.mp4", "testsrc2", 6),
        ("07_beach.mp4", "smptebars", 8),
        ("12_december-lights.mp4", "mandelbrot", 6),
    ]
    for name, source, seconds in recipes:
        dest = CLIPS / name
        # Length comes from -t rather than a per-source `duration=` option: the lavfi sources do
        # not all support one (mandelbrot does not), and -t works for every source.
        subprocess.run(  # noqa: S603 - argv list, fixed inputs
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"{source}=size=1280x720:rate=25",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                str(seconds),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-shortest",
                str(dest),
            ],
            check=True,
        )
        written.append(dest)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="delete fixtures/ first")
    args = parser.parse_args(argv)

    if args.clean and FIXTURES.exists():
        shutil.rmtree(PHOTOS, ignore_errors=True)
        shutil.rmtree(CLIPS, ignore_errors=True)

    try:
        photos = make_photos()
    except ImportError:
        print("Pillow is missing — run `uv sync` to install the dev group", file=sys.stderr)
        return 1

    clips = make_clips()
    print(f"wrote {len(photos)} photos to {PHOTOS.relative_to(REPO_ROOT)}")
    print(f"wrote {len(clips)} clips to {CLIPS.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
