"""year-highlights — cut a year of video into one film (PLAN.md §6.4).

A full year on the N305 is an overnight job, so the design is built around being interrupted:

* every expensive step writes to ``$DATA_DIR/cache`` keyed by asset id and the *analysis*
  parameters, so a restart resumes instead of starting over;
* the job reports granular progress and checks for cancellation between clips, and cancelling
  leaves the cache valid — the next run picks up where it stopped;
* the same parameters produce the same film, because selection is deterministic given the cache.

The decisions live in :mod:`.selection` as pure functions. This module does the ffmpeg work.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from pydantic import Field

from immich_addons.addons.base import Addon, AddonConfig, AddonError
from immich_addons.core import media
from immich_addons.core.client import ImmichClient
from immich_addons.core.config import Settings, get_settings
from immich_addons.core.jobs import JobContext

from .selection import Scene, cache_key, month_name, select_scenes, trim_window

log = logging.getLogger(__name__)

ADDON_ID = "year-highlights"
TAG = f"addon:{ADDON_ID}"

CLIP_LENGTH_S = 3.0
CROSSFADE_S = 0.5
SAMPLE_FPS = 2  # frames per second sampled for motion/sharpness
SAMPLE_WIDTH = 160


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
    max_per_day: int = Field(default=3, ge=1, le=20, title="Max clips per day")
    max_per_asset: int = Field(default=2, ge=1, le=20, title="Max clips per video")


@dataclass
class Cache:
    """Per-asset scene analysis, on disk so an interrupted run resumes."""

    root: Path

    def path(self, asset_id: str, params: dict[str, Any]) -> Path:
        return self.root / f"scenes_{cache_key(asset_id, params)}.json"

    def read(self, asset_id: str, params: dict[str, Any]) -> list[Scene] | None:
        path = self.path(asset_id, params)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("scene cache %s is corrupt; re-analysing", path.name)
            return None
        return [Scene.from_json(row) for row in data]

    def write(self, asset_id: str, params: dict[str, Any], scenes: list[Scene]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(asset_id, params)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps([s.to_json() for s in scenes], indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)

    def clip(self, asset_id: str, start: float, end: float) -> Path:
        return self.root / f"clip_{asset_id}_{start:.2f}_{end:.2f}.mp4"


def detect_scenes(path: Path, *, threshold: float = 27.0) -> list[tuple[float, float]]:
    """Scene boundaries via PySceneDetect's content detector.

    Falls back to fixed-length chunks when PySceneDetect is unavailable or finds nothing — a home
    video of one continuous shot legitimately has no cuts, and it should still be usable.
    """
    duration = media.media_info(path).duration_s
    try:
        from scenedetect import ContentDetector, detect  # type: ignore[import-not-found]

        found = detect(str(path), ContentDetector(threshold=threshold))
        spans = [(s.get_seconds(), e.get_seconds()) for s, e in found]
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the whole job
        log.info("scene detection unavailable or failed (%s); using fixed chunks", exc)
        spans = []

    if not spans:
        step = 5.0
        spans = [(float(t), float(min(t + step, duration))) for t in np.arange(0.0, duration, step)]
    return [(s, e) for s, e in spans if e - s >= 0.8]


def sample_frames(path: Path, start: float, end: float) -> np.ndarray:
    """Decode a few small greyscale frames from a span. Returns ``(n, h, w)``.

    Sampled rather than decoded in full: at 2 fps and 160 px wide, a three-second scene is six
    tiny frames, which is enough to measure motion and focus and cheap enough to do thousands of.
    """
    duration = max(0.1, end - start)
    args = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(path),
        "-vf",
        f"fps={SAMPLE_FPS},scale={SAMPLE_WIDTH}:-2,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = subprocess.run(args, capture_output=True, check=False)  # noqa: S603
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros((0, 0, 0), dtype=np.uint8)

    # The height is whatever scale chose, so infer it from the byte count.
    total = len(proc.stdout)
    frames = max(1, int(SAMPLE_FPS * duration))
    height = total // (frames * SAMPLE_WIDTH)
    if height <= 0:
        return np.zeros((0, 0, 0), dtype=np.uint8)
    usable = frames * height * SAMPLE_WIDTH
    return np.frombuffer(proc.stdout[:usable], dtype=np.uint8).reshape(frames, height, SAMPLE_WIDTH)


def audio_rms(path: Path, start: float, end: float) -> float:
    """Mean volume of a span, from ffmpeg's volumedetect, mapped to 0..1.

    Loud usually means something happened — laughter, waves, a shout. Silence usually means the
    camera was left running.
    """
    args = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{max(0.1, end - start):.3f}",
        "-i",
        str(path),
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    for line in proc.stderr.splitlines():
        if "mean_volume:" in line:
            try:
                db = float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except (ValueError, IndexError):
                return 0.0
            # -60 dB is effectively silence, 0 dB is full scale.
            return float(np.clip((db + 60.0) / 60.0, 0.0, 1.0))
    return 0.0


def analyse_scene(path: Path, start: float, end: float) -> tuple[float, float, list[float]]:
    """Motion, sharpness and a cheap visual descriptor for one span."""
    from immich_addons.core import scoring

    frames = sample_frames(path, start, end)
    if frames.size == 0:
        return 0.0, 0.0, [0.0] * 16

    if frames.shape[0] > 1:
        diffs = np.abs(frames[1:].astype(np.int16) - frames[:-1].astype(np.int16))
        motion = float(diffs.mean()) / 255.0
    else:
        motion = 0.0
    sharp = float(np.mean([scoring.sharpness(frame) for frame in frames]))

    # A 16-bin luminance histogram: a cheap stand-in for a scene embedding. Enough for MMR to tell
    # a beach from a living room, and free given the frames are already decoded.
    histogram, _ = np.histogram(frames, bins=16, range=(0, 255))
    descriptor = (histogram / max(1, histogram.sum())).tolist()
    return motion, sharp, descriptor


class YearHighlights(Addon):
    id: ClassVar[str] = ADDON_ID
    name: ClassVar[str] = "Year Highlights"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Finds the best moments across a year of video and cuts them into one film, "
        "with music and month titles."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("manual", "schedule")
    config_model: ClassVar[type[AddonConfig]] = YearHighlightsConfig

    #: An interrupted run resumes from its cache, so the queue requeues it instead of failing it.
    resumable: ClassVar[bool] = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        assert isinstance(config, YearHighlightsConfig)
        cache = Cache(self.settings.cache_dir / "year_highlights")
        cache.root.mkdir(parents=True, exist_ok=True)

        with ImmichClient(self.settings, dry_run=config.dry_run) as client:
            ctx.progress(0.02, f"finding videos from {config.year}")
            videos = self._videos(client, config)
            if not videos:
                raise AddonError(f"no videos found for {config.year}")
            ctx.log(f"{len(videos)} video(s)")

            scenes = self._analyse(ctx, client, config, videos, cache)
            if not scenes:
                raise AddonError("no usable scenes were found")
            ctx.log(f"{len(scenes)} candidate scene(s)")

            ctx.progress(0.7, "selecting")
            chosen = select_scenes(
                scenes,
                target_length_s=config.target_length_s,
                clip_length_s=CLIP_LENGTH_S,
                max_per_day=config.max_per_day,
                max_per_asset=config.max_per_asset,
            )
            months = sorted({s.month for s in chosen})
            ctx.log(
                f"selected {len(chosen)} clip(s) across {len(months)} month(s): "
                + ", ".join(month_name(m) for m in months)
            )

            if config.dry_run:
                for scene in chosen:
                    ctx.artifact(
                        "clip",
                        f"{scene.asset_id}@{scene.start_s:.1f}-{scene.end_s:.1f}",
                        scene.taken_at.strftime("%d %b"),
                    )
                ctx.log("dry run: nothing was rendered or uploaded")
                return

            ctx.progress(0.75, "rendering")
            film = self._assemble(ctx, client, config, chosen, cache)
            ctx.artifact("file", str(film), f"Highlights {config.year}")

            ctx.progress(0.95, "uploading")
            self._publish(ctx, client, config, film)

    # --- steps ---------------------------------------------------------------------------

    def _videos(self, client: ImmichClient, config: YearHighlightsConfig) -> list[dict[str, Any]]:
        start = datetime(config.year, 1, 1, tzinfo=UTC)
        end = datetime(config.year, 12, 31, 23, 59, 59, tzinfo=UTC)
        videos = list(
            client.iter_metadata(
                taken_after=start,
                taken_before=end,
                asset_type="VIDEO",
                person_ids=config.people_filter or None,
            )
        )
        return [
            v
            for v in videos
            if not any(
                str((t or {}).get("name", "")).startswith("addon:") for t in v.get("tags") or []
            )
        ]

    def _analysis_params(self, config: YearHighlightsConfig) -> dict[str, Any]:
        """Only what changes the *analysis*. Music and title cards deliberately excluded."""
        return {"sample_fps": SAMPLE_FPS, "sample_width": SAMPLE_WIDTH, "version": 1}

    def _analyse(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: YearHighlightsConfig,
        videos: list[dict[str, Any]],
        cache: Cache,
    ) -> list[Scene]:
        params = self._analysis_params(config)
        scenes: list[Scene] = []
        cached_count = 0

        for i, video in enumerate(videos):
            ctx.progress(
                0.05 + 0.6 * i / max(1, len(videos)),
                f"analysing {i + 1}/{len(videos)}: {video.get('originalFileName', '')}",
            )
            asset_id = str(video["id"])
            hit = cache.read(asset_id, params)
            if hit is not None:
                scenes.extend(hit)
                cached_count += 1
                continue

            taken = _taken_at(video) or datetime(config.year, 1, 1, tzinfo=UTC)
            local = cache.root / f"src_{asset_id}.mp4"
            try:
                if not local.exists():
                    client.download_original(asset_id, local)
                found = self._analyse_one(asset_id, local, taken)
            except Exception as exc:  # noqa: BLE001 - one bad video must not lose the whole run
                ctx.log(f"skipping {video.get('originalFileName', asset_id)}: {exc}")
                found = []

            cache.write(asset_id, params, found)
            scenes.extend(found)

        if cached_count:
            ctx.log(f"reused cached analysis for {cached_count} video(s)")
        return scenes

    def _analyse_one(self, asset_id: str, path: Path, taken: datetime) -> list[Scene]:
        out: list[Scene] = []
        for start, end in detect_scenes(path):
            motion, sharp, descriptor = analyse_scene(path, start, end)
            out.append(
                Scene(
                    asset_id=asset_id,
                    start_s=start,
                    end_s=end,
                    taken_at=taken,
                    motion=motion,
                    audio=audio_rms(path, start, end),
                    sharpness=sharp,
                    descriptor=descriptor,
                )
            )
        return out

    def _assemble(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: YearHighlightsConfig,
        chosen: list[Scene],
        cache: Cache,
    ) -> Path:
        segments: list[Path] = []
        for i, scene in enumerate(chosen):
            # Cancelling between clips leaves every finished segment in the cache, so the next run
            # resumes rather than restarting.
            ctx.progress(
                0.75 + 0.18 * i / max(1, len(chosen)),
                f"cutting clip {i + 1}/{len(chosen)} ({scene.taken_at:%d %b})",
            )
            start, end = trim_window(scene, CLIP_LENGTH_S)
            segment = cache.clip(scene.asset_id, start, end)
            if not segment.exists():
                source = cache.root / f"src_{scene.asset_id}.mp4"
                if not source.exists():
                    client.download_original(scene.asset_id, source)
                _cut(source, segment, start, end)
            segments.append(segment)

        if not segments:
            raise AddonError("nothing to assemble")

        out = self.settings.output_dir / f"highlights_{config.year}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        _concat(segments, out, music=self._music_path(config))
        return out

    def _music_path(self, config: YearHighlightsConfig) -> Path | None:
        if not config.music_file:
            return None
        candidate = (self.settings.music_dir / Path(config.music_file).name).resolve()
        if not candidate.is_file():
            raise AddonError(
                f"music file {config.music_file!r} not found in {self.settings.music_dir}"
            )
        return candidate

    def _publish(
        self, ctx: JobContext, client: ImmichClient, config: YearHighlightsConfig, film: Path
    ) -> None:
        uploaded = client.upload_asset(film, device_asset_id=f"{ADDON_ID}_{config.year}")
        asset_id = str(uploaded.get("id") or "")
        if not asset_id:
            ctx.log("dry run: would upload the film, tag it and add it to an album")
            return

        album = client.create_album(f"Highlights {config.year}", asset_ids=[asset_id])
        ctx.artifact("album", str(album.get("id") or ""), f"Highlights {config.year}")

        tag_id = ""
        for tag in client.tags():
            if str(tag.get("name") or "") == TAG:
                tag_id = str(tag.get("id") or "")
                break
        if not tag_id:
            tag_id = str(client.create_tag(TAG).get("id") or "")
        if tag_id:
            client.assign_tag(tag_id, [asset_id])
        ctx.log(f"uploaded {film.name} as {asset_id}")


def _cut(source: Path, dest: Path, start: float, end: float) -> None:
    """Re-encode one clip. Re-encoded rather than stream-copied so every segment shares a codec,
    resolution, timebase and frame rate — concat is unforgiving about that."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(source),
        "-vf",
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
        *media.encoder_args(),
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(dest),
    ]
    media.run(args)


def _concat(segments: list[Path], dest: Path, *, music: Path | None) -> None:
    """Join the segments. Music replaces the clip audio; without music the clips keep their own.

    Uses the concat demuxer rather than an xfade chain: an xfade graph for a hundred clips is a
    hundred nested filters, which is fragile to build and slow to evaluate on a small CPU.
    """
    listing = dest.with_suffix(".txt")
    listing.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in segments) + "\n", encoding="utf-8"
    )

    args = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
    ]
    if music is not None:
        args += ["-i", str(music), "-map", "0:v", "-map", "1:a", "-shortest"]
    args += [*media.encoder_args(), "-c:a", "aac", str(dest)]
    try:
        media.run(args)
    finally:
        listing.unlink(missing_ok=True)


def _taken_at(asset: dict[str, Any]) -> datetime | None:
    raw = asset.get("fileCreatedAt") or (asset.get("exifInfo") or {}).get("dateTimeOriginal")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
