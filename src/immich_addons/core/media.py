"""ffmpeg / ffprobe wrappers.

Hardware encoding is opportunistic: the NAS has an Intel iGPU, so we use ``h264_qsv`` when
``/dev/dri`` is passed into the container *and* the packaged ffmpeg was built with QSV. Otherwise
everything falls back to ``libx264`` and just takes longer — never fails.

Nothing here writes to a source file: every operation takes an input path and a distinct output
path (CLAUDE.md, "never modify original assets").
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

QSV_DEVICE = Path("/dev/dri")
QSV_ENCODER = "h264_qsv"
SOFTWARE_ENCODER = "libx264"

#: stderr tail kept on failure — ffmpeg is verbose and the useful line is always near the end.
STDERR_TAIL_LINES = 12


class MediaError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed."""


class MediaToolMissingError(MediaError):
    """ffmpeg or ffprobe is not on PATH (the hub image installs both)."""


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    width: int
    height: int
    codec: str
    has_audio: bool

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaToolMissingError(f"{name} not found on PATH")
    return path


def run(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg-family command, raising :class:`MediaError` with the useful stderr lines."""
    log.debug("exec: %s", " ".join(args))
    proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
        raise MediaError(f"{Path(args[0]).name} exited {proc.returncode}:\n{tail}")
    return proc


@lru_cache(maxsize=1)
def ffmpeg_encoders() -> frozenset[str]:
    """Encoder names this ffmpeg build supports."""
    try:
        proc = run([_tool("ffmpeg"), "-hide_banner", "-encoders"])
    except MediaError:
        return frozenset()
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        # rows look like: " V....D h264_qsv    H.264 (Intel Quick Sync Video acceleration)"
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


@lru_cache(maxsize=1)
def has_qsv() -> bool:
    """True when Quick Sync is both present on the host and built into ffmpeg."""
    if not QSV_DEVICE.exists():
        log.info("%s missing — no QSV, falling back to %s", QSV_DEVICE, SOFTWARE_ENCODER)
        return False
    if QSV_ENCODER not in ffmpeg_encoders():
        log.info("ffmpeg has no %s encoder — falling back to %s", QSV_ENCODER, SOFTWARE_ENCODER)
        return False
    return True


def video_encoder() -> str:
    """``h264_qsv`` when available, else ``libx264``."""
    return QSV_ENCODER if has_qsv() else SOFTWARE_ENCODER


def encoder_args(*, crf: int = 20, qsv_quality: int = 24) -> list[str]:
    """Encoder selection plus its quality flag — the two differ between QSV and x264."""
    if video_encoder() == QSV_ENCODER:
        return ["-c:v", QSV_ENCODER, "-global_quality", str(qsv_quality), "-preset", "medium"]
    return ["-c:v", SOFTWARE_ENCODER, "-crf", str(crf), "-preset", "medium"]


def ffprobe(path: Path) -> dict[str, Any]:
    """Raw ffprobe JSON for a file."""
    proc = run(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    return json.loads(proc.stdout)


def media_info(path: Path) -> MediaInfo:
    """The handful of ffprobe fields the addons actually use."""
    data = ffprobe(path)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"no video stream in {path.name}")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)
    return MediaInfo(
        duration_s=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        codec=str(video.get("codec_name") or ""),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def apply_lut(
    src: Path,
    dest: Path,
    lut_path: Path,
    *,
    intensity: float = 1.0,
    is_video: bool = False,
    jpeg_quality: int = 3,
) -> Path:
    """Write a LUT-graded copy of ``src`` to ``dest``. ``src`` is never touched.

    ``intensity`` blends the graded result over the original, so 0.0 is a no-op and 1.0 is the LUT
    at full strength. Used by auto-lut (PLAN.md §6.1); lives here because it is a plain ffmpeg
    filter-graph wrapper.
    """
    if not 0.0 <= intensity <= 1.0:
        raise ValueError(f"intensity must be within 0..1, got {intensity}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    lut = str(lut_path).replace("\\", "/").replace(":", r"\:")

    if intensity >= 1.0:
        filtergraph = f"lut3d=file='{lut}'"
    else:
        filtergraph = (
            f"[0:v]split=2[base][grade];"
            f"[grade]lut3d=file='{lut}'[graded];"
            f"[base][graded]blend=all_mode=normal:all_opacity={intensity}"
        )
    flag = "-filter_complex" if intensity < 1.0 else "-vf"

    args = [_tool("ffmpeg"), "-hide_banner", "-y", "-i", str(src), flag, filtergraph]
    if is_video:
        args += [*encoder_args(), "-c:a", "copy"]
    else:
        args += ["-q:v", str(jpeg_quality)]
    args.append(str(dest))
    run(args)
    return dest
