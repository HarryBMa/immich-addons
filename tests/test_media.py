"""ffmpeg wrappers, with subprocess mocked — CI has no /dev/dri and may have no ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from immich_addons.core import media

ENCODERS_STDOUT = """Encoders:
 V..... = Video
 ------
 V....D h264_qsv             H.264 (Intel Quick Sync Video acceleration)
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC
 A..... aac                  AAC (Advanced Audio Coding)
"""


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    media.ffmpeg_encoders.cache_clear()
    media.has_qsv.cache_clear()


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record argv instead of executing, and pretend both tools are on PATH."""
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        stdout = ENCODERS_STDOUT if "-encoders" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(media.subprocess, "run", _run)
    monkeypatch.setattr(media.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_missing_tool_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    with pytest.raises(media.MediaToolMissingError, match="ffmpeg"):
        media.run([media._tool("ffmpeg")])


def test_failure_keeps_only_the_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    noise = "\n".join(f"noise {i}" for i in range(50))

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", noise + "\nInvalid argument")

    monkeypatch.setattr(media.subprocess, "run", _run)
    with pytest.raises(media.MediaError) as excinfo:
        media.run(["/usr/bin/ffmpeg", "-i", "x"])
    message = str(excinfo.value)
    assert "Invalid argument" in message
    assert "noise 0" not in message


def test_encoder_list_is_parsed(fake_run: list[list[str]]) -> None:
    assert {"h264_qsv", "libx264", "aac"} <= media.ffmpeg_encoders()


def test_qsv_used_when_it_is_present_and_actually_works(
    fake_run: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media.Path, "exists", lambda self: True)
    assert media.has_qsv() is True
    assert media.video_encoder() == "h264_qsv"
    assert "-global_quality" in media.encoder_args()


def test_qsv_is_proved_by_encoding_a_frame_not_by_a_device_node(
    fake_run: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host can have /dev/dri and a QSV-capable ffmpeg and still fail every encode with
    'Error creating a MFX session' — no Intel GPU behind the node, or a driver mismatch. Presence
    is not capability, so the probe encodes one frame."""
    monkeypatch.setattr(media.Path, "exists", lambda self: True)
    media.has_qsv()

    probe = [call for call in fake_run if "-c:v" in call and media.QSV_ENCODER in call]
    assert probe, "has_qsv() must try a real encode"
    assert probe[0][-1] == "-" and "null" in probe[0], "and throw the result away"


def test_falls_back_when_qsv_is_present_but_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CI runner is exactly this case, and so is a NAS with /dev/dri passed to a container
    that has no working driver."""
    monkeypatch.setattr(media.Path, "exists", lambda self: True)
    monkeypatch.setattr(media, "ffmpeg_encoders", lambda: frozenset({"h264_qsv", "libx264"}))
    monkeypatch.setattr(media, "qsv_encodes_a_frame", lambda: False)

    assert media.has_qsv() is False
    assert media.video_encoder() == "libx264"


def test_a_failing_qsv_probe_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(args: list[str], **kwargs: object) -> None:
        raise media.MediaError("ffmpeg exited 171:\nError creating a MFX session: -9.")

    monkeypatch.setattr(media, "_tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media, "run", explode)
    assert media.qsv_encodes_a_frame() is False


def test_a_hanging_qsv_probe_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged driver must cost 30 seconds once, not block the first encode forever."""

    def hang(args: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(media, "_tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media, "run", hang)
    assert media.qsv_encodes_a_frame() is False


def test_falls_back_to_libx264_without_dev_dri(
    fake_run: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media.Path, "exists", lambda self: False)
    assert media.has_qsv() is False
    assert media.video_encoder() == "libx264"
    assert "-crf" in media.encoder_args()


def test_falls_back_when_ffmpeg_lacks_the_qsv_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media.Path, "exists", lambda self: True)
    monkeypatch.setattr(media, "ffmpeg_encoders", lambda: frozenset({"libx264"}))
    assert media.has_qsv() is False


def test_media_info_reads_the_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media,
        "ffprobe",
        lambda path: {
            "format": {"duration": "12.5"},
            "streams": [
                {"codec_type": "video", "width": 1080, "height": 1920, "codec_name": "hevc"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        },
    )
    info = media.media_info(Path("clip.mov"))
    assert info.duration_s == 12.5
    assert info.is_portrait is True
    assert info.has_audio is True
    assert info.codec == "hevc"


def test_media_info_rejects_a_file_without_video(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "ffprobe", lambda path: {"streams": [{"codec_type": "audio"}]})
    with pytest.raises(media.MediaError, match="no video stream"):
        media.media_info(Path("music.mp3"))


def test_apply_lut_at_full_intensity_uses_a_simple_filter(
    fake_run: list[list[str]], tmp_path: Path
) -> None:
    media.apply_lut(tmp_path / "in.jpg", tmp_path / "out.jpg", tmp_path / "kodak.cube")
    args = fake_run[-1]
    assert "-vf" in args
    assert "lut3d=file=" in args[args.index("-vf") + 1]
    assert "blend" not in " ".join(args)


def test_apply_lut_blends_at_partial_intensity(fake_run: list[list[str]], tmp_path: Path) -> None:
    media.apply_lut(
        tmp_path / "in.jpg", tmp_path / "out.jpg", tmp_path / "kodak.cube", intensity=0.4
    )
    graph = " ".join(fake_run[-1])
    assert "all_opacity=0.4" in graph
    assert "split=2" in graph


def test_apply_lut_never_writes_over_the_source(fake_run: list[list[str]], tmp_path: Path) -> None:
    src = tmp_path / "in.jpg"
    media.apply_lut(src, tmp_path / "out.jpg", tmp_path / "kodak.cube")
    args = fake_run[-1]
    assert args[-1] == str(tmp_path / "out.jpg")
    assert args.count(str(src)) == 1  # only as the -i input


def test_apply_lut_rejects_an_out_of_range_intensity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="0..1"):
        media.apply_lut(tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "l.cube", intensity=1.5)
