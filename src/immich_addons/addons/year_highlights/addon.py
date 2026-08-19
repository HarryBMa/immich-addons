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
PHOTO_LENGTH_S = 2.0
TITLE_LENGTH_S = 1.5
CROSSFADE_S = 0.5
SAMPLE_FPS = 2  # frames per second sampled for motion/sharpness
SAMPLE_WIDTH = 160

#: Ceiling on stills examined. A family year holds tens of thousands of photos and at most a
#: handful reach the film, so the candidates are thinned across the year before any decoding.
MAX_PHOTO_CANDIDATES = 400

#: Every segment is normalised to this, because the concat demuxer refuses to join streams that
#: disagree about any of it — including the title cards and the stills.
FRAME_SIZE = "1920:1080"
FRAME_RATE = 30
AUDIO_RATE = 48000

#: Fonts for the month cards, in preference order. The hub image installs fonts-dejavu (it is a
#: WeasyPrint dependency anyway); the Windows entry is for running the addon off a workstation.
FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


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
    keep_clip_audio: bool = Field(
        default=True,
        title="Keep the clips' own sound",
        description=(
            "Mix the clips' audio over the music and duck the music under it. "
            "Off means music only. Ignored when no music is chosen."
        ),
    )
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

    def still(self, asset_id: str) -> Path:
        return self.root / f"still_{asset_id}.jpg"

    def title(self, month: int) -> Path:
        return self.root / f"title_{month:02d}.mp4"


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
            if config.include_photos:
                photos = self._photos(client, config)
                ctx.log(f"{len(photos)} photo(s) to consider")
                scenes += self._analyse_photos(ctx, client, config, photos, cache)
            if not scenes:
                raise AddonError("no usable scenes were found")
            ctx.log(f"{len(scenes)} candidate scene(s)")

            ctx.progress(0.7, "selecting")
            chosen = select_scenes(
                scenes,
                target_length_s=config.target_length_s,
                clip_length_s=CLIP_LENGTH_S,
                photo_length_s=PHOTO_LENGTH_S,
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
                        "clip" if not scene.is_photo else "still",
                        f"{scene.asset_id}@{scene.start_s:.1f}-{scene.end_s:.1f}",
                        scene.taken_at.strftime("%d %b"),
                    )
                stills = sum(1 for s in chosen if s.is_photo)
                ctx.log(
                    f"dry run: nothing was rendered or uploaded "
                    f"({len(chosen) - stills} clip(s), {stills} still(s))"
                )
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

    def _photos(self, client: ImmichClient, config: YearHighlightsConfig) -> list[dict[str, Any]]:
        """The year's stills, same filters as the video. Capped, because a family year can hold
        twenty thousand photos and only a handful will ever reach the film."""
        start = datetime(config.year, 1, 1, tzinfo=UTC)
        end = datetime(config.year, 12, 31, 23, 59, 59, tzinfo=UTC)
        photos = [
            p
            for p in client.iter_metadata(
                taken_after=start,
                taken_before=end,
                asset_type="IMAGE",
                person_ids=config.people_filter or None,
            )
            if not any(
                str((t or {}).get("name", "")).startswith("addon:") for t in p.get("tags") or []
            )
        ]
        if len(photos) <= MAX_PHOTO_CANDIDATES:
            return photos
        # Thin them evenly across the year rather than taking the first N, which would be January.
        step = len(photos) / MAX_PHOTO_CANDIDATES
        return [photos[int(i * step)] for i in range(MAX_PHOTO_CANDIDATES)]

    def _analyse_photos(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: YearHighlightsConfig,
        photos: list[dict[str, Any]],
        cache: Cache,
    ) -> list[Scene]:
        """One scene per still, scored on sharpness alone — a photo has no motion or audio.

        Works from the preview rather than the original: it is already a JPEG whatever the camera
        shot, which keeps HEIC and RAW usable, and 2 seconds of slow zoom does not need more.
        """
        params = {**self._analysis_params(config), "kind": "photo"}
        scenes: list[Scene] = []

        for i, photo in enumerate(photos):
            if i % 25 == 0:
                ctx.progress(0.62 + 0.06 * i / max(1, len(photos)), f"reading photo {i + 1}")
            asset_id = str(photo["id"])
            hit = cache.read(asset_id, params)
            if hit is not None:
                scenes.extend(hit)
                continue

            taken = _taken_at(photo) or datetime(config.year, 1, 1, tzinfo=UTC)
            local = cache.still(asset_id)
            found: list[Scene] = []
            try:
                if not local.exists():
                    local.write_bytes(client.thumbnail(asset_id, size="preview"))
                _, sharp, descriptor = analyse_scene(local, 0.0, 0.4)
                found = [
                    Scene(
                        asset_id=asset_id,
                        start_s=0.0,
                        end_s=PHOTO_LENGTH_S,
                        taken_at=taken,
                        sharpness=sharp,
                        descriptor=descriptor,
                        kind="photo",
                    )
                ]
            except Exception as exc:  # noqa: BLE001 - one bad photo must not lose the run
                ctx.log(f"skipping photo {photo.get('originalFileName', asset_id)}: {exc}")

            cache.write(asset_id, params, found)
            scenes.extend(found)

        return scenes

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
        titled: set[int] = set()

        for i, scene in enumerate(chosen):
            # Cancelling between clips leaves every finished segment in the cache, so the next run
            # resumes rather than restarting.
            ctx.progress(
                0.75 + 0.18 * i / max(1, len(chosen)),
                f"cutting {i + 1}/{len(chosen)} ({scene.taken_at:%d %b})",
            )

            if config.month_titles and scene.month not in titled:
                titled.add(scene.month)
                card = self._title_segment(ctx, cache, scene.month)
                if card is not None:
                    segments.append(card)

            segments.append(self._segment(client, cache, scene))

        if not segments:
            raise AddonError("nothing to assemble")

        music = self._music_path(config)
        out = self.settings.output_dir / f"highlights_{config.year}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        ctx.progress(0.93, f"encoding {len(segments)} segment(s)")
        _concat(segments, out, music=music, duck=config.keep_clip_audio and music is not None)
        return out

    def _segment(self, client: ImmichClient, cache: Cache, scene: Scene) -> Path:
        """One rendered piece of film: a trimmed clip, or a still with a slow push-in."""
        if scene.is_photo:
            segment = cache.clip(scene.asset_id, 0.0, PHOTO_LENGTH_S)
            if not segment.exists():
                source = cache.still(scene.asset_id)
                if not source.exists():
                    source.write_bytes(client.thumbnail(scene.asset_id, size="preview"))
                _ken_burns(source, segment)
            return segment

        start, end = trim_window(scene, CLIP_LENGTH_S)
        segment = cache.clip(scene.asset_id, start, end)
        if not segment.exists():
            source = cache.root / f"src_{scene.asset_id}.mp4"
            if not source.exists():
                client.download_original(scene.asset_id, source)
            _cut(source, segment, start, end)
        return segment

    def _title_segment(self, ctx: JobContext, cache: Cache, month: int) -> Path | None:
        """A month card, or ``None`` if this box has no font — worth losing the cards over, not
        the film."""
        card = cache.title(month)
        if card.exists():
            return card
        try:
            _title_card(card, month_name(month))
        except AddonError as exc:
            ctx.log(f"month title cards disabled: {exc}")
            return None
        return card

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
        f"scale={FRAME_SIZE}:force_original_aspect_ratio=decrease,"
        f"pad={FRAME_SIZE}:(ow-iw)/2:(oh-ih)/2,fps={FRAME_RATE},setsar=1",
        *media.encoder_args(),
        "-c:a",
        "aac",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "2",
        str(dest),
    ]
    media.run(args)


def font_file() -> str | None:
    """The first usable font for the month cards, or ``None`` if the box has none."""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _escape_for_filter(text: str) -> str:
    r"""Escape a value going inside an ffmpeg filter argument.

    ``:`` separates filter options and ``\`` escapes, so a Windows font path
    (``C:/Windows/…``) silently truncates the filter unless both are escaped.
    """
    return text.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _title_card(dest: Path, text: str, *, seconds: float = TITLE_LENGTH_S) -> None:
    """Render a month card: white text on black, with silent audio.

    The silence is not decoration — a segment without an audio stream makes the concat demuxer
    drop audio for everything after it.
    """
    font = font_file()
    if font is None:
        raise AddonError("no usable font for the month title cards")

    dest.parent.mkdir(parents=True, exist_ok=True)
    width, height = FRAME_SIZE.split(":")
    drawtext = (
        f"drawtext=fontfile='{_escape_for_filter(font)}'"
        f":text='{_escape_for_filter(text)}'"
        ":fontcolor=white:fontsize=96:x=(w-text_w)/2:y=(h-text_h)/2"
    )
    args = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={FRAME_RATE}:d={seconds:.2f}",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={AUDIO_RATE}:cl=stereo",
        "-vf",
        f"{drawtext},setsar=1",
        "-t",
        f"{seconds:.2f}",
        *media.encoder_args(),
        "-c:a",
        "aac",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "2",
        str(dest),
    ]
    media.run(args)


def _ken_burns(source: Path, dest: Path, *, seconds: float = PHOTO_LENGTH_S) -> None:
    """Turn a still into a slow push-in of ``seconds``, matching the video segments exactly.

    The image is scaled to twice the output first: ``zoompan`` samples from its input, so zooming a
    1080p source into a 1080p frame is a straight upscale and looks soft.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    width, height = FRAME_SIZE.split(":")
    frames = max(1, int(seconds * FRAME_RATE))
    zoom_per_frame = 0.12 / frames  # a 12% push over the whole clip: perceptible, not seasick
    filters = (
        f"scale={int(width) * 2}:{int(height) * 2}:force_original_aspect_ratio=decrease,"
        f"pad={int(width) * 2}:{int(height) * 2}:(ow-iw)/2:(oh-ih)/2,"
        f"zoompan=z='min(zoom+{zoom_per_frame:.6f},1.12)'"
        ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={width}x{height}:fps={FRAME_RATE},"
        "setsar=1"
    )
    args = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-t",
        f"{seconds:.2f}",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={AUDIO_RATE}:cl=stereo",
        "-vf",
        filters,
        "-t",
        f"{seconds:.2f}",
        *media.encoder_args(),
        "-c:a",
        "aac",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "2",
        str(dest),
    ]
    media.run(args)


#: Ducking: the music is compressed, keyed by the clips' own audio. Slow release so the music
#: swells back between words rather than pumping on every syllable.
DUCK_FILTER = (
    "[0:a]asplit=2[key][clip];"
    "[1:a][key]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=500[ducked];"
    "[ducked][clip]amix=inputs=2:duration=first:dropout_transition=0[a]"
)


def _concat(segments: list[Path], dest: Path, *, music: Path | None, duck: bool = False) -> None:
    """Join the segments.

    Three audio outcomes: no music keeps the clips' own sound; music with ``duck`` mixes both and
    pulls the music down under the clips; music without it replaces the clip audio entirely.

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
        # Loop the music: a three-minute film and a two-minute track should not end early.
        # -shortest then cuts the loop at the end of the video.
        args += ["-stream_loop", "-1", "-i", str(music)]
        if duck:
            args += ["-filter_complex", DUCK_FILTER, "-map", "0:v", "-map", "[a]"]
        else:
            args += ["-map", "0:v", "-map", "1:a"]
        args += ["-shortest"]
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
