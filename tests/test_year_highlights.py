"""year-highlights: quota-constrained selection, cache identity, and a real ffmpeg assembly."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from immich_addons.addons.base import AddonError
from immich_addons.addons.year_highlights.addon import (
    Cache,
    YearHighlights,
    YearHighlightsConfig,
    _concat,
    _cut,
    _ken_burns,
    _title_card,
    analyse_scene,
    audio_rms,
    detect_scenes,
    sample_frames,
)
from immich_addons.addons.year_highlights.selection import (
    Scene,
    cache_key,
    descriptors,
    month_name,
    score_scenes,
    select_scenes,
    trim_window,
)
from immich_addons.core.client import ImmichClient
from immich_addons.core.jobs import JobQueue, JobStatus

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
FIXTURE_CLIPS = Path(__file__).resolve().parents[1] / "fixtures" / "clips"


def scene(
    asset: str = "v1",
    *,
    month: int = 6,
    day: int = 1,
    start: float = 0.0,
    length: float = 5.0,
    motion: float = 0.5,
    audio: float = 0.5,
    sharpness: float = 0.5,
    descriptor: list[float] | None = None,
    kind: str = "video",
) -> Scene:
    return Scene(
        asset_id=asset,
        start_s=start,
        end_s=start + length,
        taken_at=datetime(2026, month, day, 12, tzinfo=UTC),
        motion=motion,
        audio=audio,
        sharpness=sharpness,
        # `or` would turn an explicitly empty descriptor back into the default, which is exactly
        # the case one of the tests needs.
        descriptor=[1.0, 0.0, 0.0, 0.0] if descriptor is None else descriptor,
        kind=kind,
    )


# --- scoring --------------------------------------------------------------------------------


def test_a_lively_scene_outscores_a_still_one() -> None:
    lively = scene(motion=0.9, audio=0.9, sharpness=0.8)
    still = scene(motion=0.0, audio=0.0, sharpness=0.8)
    scores = score_scenes([lively, still])
    assert scores[0] > scores[1]


def test_scores_use_rank_not_magnitude() -> None:
    """Motion and audio scales differ between a quiet indoor clip and a windy beach clip, so only
    the ordering can be trusted."""
    modest = score_scenes([scene(motion=0.1), scene(motion=0.2), scene(motion=0.3)])
    extreme = score_scenes([scene(motion=0.1), scene(motion=0.2), scene(motion=99.0)])
    np.testing.assert_allclose(modest, extreme)


def test_scoring_an_empty_list() -> None:
    assert score_scenes([]).size == 0


# --- selection ------------------------------------------------------------------------------


def test_one_long_recording_cannot_take_over_the_film() -> None:
    """The quota that matters: a birthday party holds more good footage than three quiet months."""
    party = [scene("party", month=6, day=12, start=i * 10, motion=0.95) for i in range(20)]
    rest = [scene(f"v{m}", month=m, day=3, motion=0.4) for m in (1, 3, 9, 11)]
    chosen = select_scenes(party + rest, target_length_s=30, max_per_asset=2)

    from_party = [s for s in chosen if s.asset_id == "party"]
    assert len(from_party) <= 2


def test_the_per_day_quota_is_enforced() -> None:
    same_day = [scene(f"v{i}", month=5, day=4, motion=0.9) for i in range(10)]
    chosen = select_scenes(same_day, target_length_s=60, max_per_day=3)
    assert len(chosen) <= 3


def test_every_month_with_footage_is_represented() -> None:
    """§6.4 acceptance: clips from every fixture 'month'."""
    scenes = [scene(f"v{m}", month=m, day=5, motion=0.2 + m / 100) for m in range(1, 13)]
    chosen = select_scenes(scenes, target_length_s=60)
    assert sorted({s.month for s in chosen}) == list(range(1, 13))


def test_coverage_wins_over_raw_quality() -> None:
    """A weak January clip beats a second strong July clip, because a year film spans the year."""
    scenes = [
        scene("jul-a", month=7, day=1, motion=0.99),
        scene("jul-b", month=7, day=2, motion=0.98),
        scene("jan", month=1, day=1, motion=0.05),
    ]
    chosen = select_scenes(scenes, target_length_s=6)
    assert {s.month for s in chosen} == {1, 7}


def test_the_film_runs_forwards() -> None:
    scenes = [scene(f"v{m}", month=m, day=2) for m in (11, 2, 7, 4)]
    chosen = select_scenes(scenes, target_length_s=60)
    assert [s.month for s in chosen] == sorted(s.month for s in chosen)


def test_target_length_bounds_the_clip_count() -> None:
    scenes = [scene(f"v{i}", month=(i % 12) + 1, day=1 + i % 20) for i in range(200)]
    chosen = select_scenes(scenes, target_length_s=30, clip_length_s=3.0)
    assert len(chosen) <= 12


def test_selection_is_deterministic() -> None:
    """§6.4 acceptance: the same params produce the same film."""
    scenes = [scene(f"v{i}", month=(i % 12) + 1, day=1 + i % 25, motion=i / 50) for i in range(60)]
    first = select_scenes(scenes, target_length_s=45)
    second = select_scenes(scenes, target_length_s=45)
    assert [(s.asset_id, s.start_s) for s in first] == [(s.asset_id, s.start_s) for s in second]


def test_selecting_from_nothing() -> None:
    assert select_scenes([], target_length_s=60) == []


def test_descriptors_never_make_two_scenes_look_identical() -> None:
    """A missing descriptor must not collapse unrelated scenes into one MMR group."""
    rows = descriptors([scene(descriptor=[]), scene(descriptor=[])])
    assert not np.allclose(rows[0], rows[1])


# --- trimming and cache identity -------------------------------------------------------------


def test_the_trim_is_centred() -> None:
    """Scene edges hold the camera settling or turning away, so take the middle."""
    start, end = trim_window(scene(start=10.0, length=9.0), 3.0)
    assert (start, end) == (13.0, 16.0)


def test_a_short_scene_is_kept_whole() -> None:
    assert trim_window(scene(start=4.0, length=2.0), 3.0) == (4.0, 6.0)


def test_cache_key_changes_with_the_asset_and_the_analysis_params() -> None:
    base = {"sample_fps": 2, "version": 1}
    assert cache_key("a", base) != cache_key("b", base)
    assert cache_key("a", base) != cache_key("a", {"sample_fps": 4, "version": 1})
    assert cache_key("a", base) == cache_key("a", dict(reversed(list(base.items()))))


def test_music_and_length_do_not_invalidate_the_analysis_cache(settings) -> None:  # noqa: ANN001
    """The reason resume is cheap: re-running with different music reuses every analysis."""
    addon = YearHighlights(settings)
    a = addon._analysis_params(YearHighlightsConfig(music_file="a.mp3", target_length_s=60))
    b = addon._analysis_params(YearHighlightsConfig(music_file="b.mp3", target_length_s=600))
    assert cache_key("v1", a) == cache_key("v1", b)


def test_cache_round_trips_scenes(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    params = {"version": 1}
    assert cache.read("v1", params) is None

    cache.write("v1", params, [scene(), scene(start=9.0)])
    restored = cache.read("v1", params)

    assert restored is not None
    assert len(restored) == 2
    assert restored[1].start_s == 9.0
    assert restored[0].taken_at.tzinfo is not None


def test_a_corrupt_cache_entry_is_re_analysed(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    params = {"version": 1}
    cache.write("v1", params, [scene()])
    cache.path("v1", params).write_text("{broken", encoding="utf-8")
    assert cache.read("v1", params) is None


def test_month_names() -> None:
    assert month_name(1) == "January"
    assert month_name(12) == "December"


# --- ffmpeg-backed analysis ------------------------------------------------------------------

pytestmark_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A six-second clip with real motion and a tone, made with ffmpeg."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg is not installed")
    dest = tmp_path_factory.mktemp("yh") / "clip.mp4"
    import subprocess

    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=True,
    )
    return dest


@pytestmark_ffmpeg
def test_frames_are_sampled_at_the_expected_shape(clip: Path) -> None:
    frames = sample_frames(clip, 1.0, 3.0)
    assert frames.ndim == 3
    assert frames.shape[0] >= 2
    assert frames.shape[2] == 160


@pytestmark_ffmpeg
def test_motion_is_detected_in_a_moving_clip(clip: Path) -> None:
    motion, sharp, descriptor = analyse_scene(clip, 1.0, 4.0)
    assert motion > 0.0
    assert sharp > 0.0
    assert len(descriptor) == 16
    assert descriptor == pytest.approx(descriptor)  # finite
    assert sum(descriptor) == pytest.approx(1.0, abs=1e-6)


@pytestmark_ffmpeg
def test_audio_level_is_read(clip: Path) -> None:
    assert 0.0 < audio_rms(clip, 1.0, 3.0) <= 1.0


@pytestmark_ffmpeg
def test_a_missing_span_degrades_to_zero(clip: Path) -> None:
    motion, sharp, _ = analyse_scene(clip, 9999.0, 10000.0)
    assert motion == 0.0
    assert sharp == 0.0


@pytestmark_ffmpeg
def test_scene_detection_returns_usable_spans(clip: Path) -> None:
    spans = detect_scenes(clip)
    assert spans
    assert all(end > start for start, end in spans)
    assert all(end - start >= 0.8 for start, end in spans)


def test_a_still_would_lose_a_like_for_like_contest() -> None:
    """Why stills get their own pool: motion and audio are 60% of the score and a photo has
    neither, so in one pool even a perfect photo loses to mediocre video."""
    photo = scene(asset="p1", kind="photo", motion=0.0, audio=0.0, sharpness=1.0)
    mediocre_video = scene(asset="v1", motion=0.3, audio=0.3, sharpness=0.1)

    scores = score_scenes([photo, mediocre_video])
    assert scores[1] > scores[0]


def test_stills_are_selected_alongside_video_not_against_it() -> None:
    photos = [scene(asset=f"p{i}", kind="photo", month=i, sharpness=0.9) for i in range(1, 7)]
    videos = [scene(asset=f"v{i}", month=i) for i in range(1, 7)]

    chosen = select_scenes(photos + videos, target_length_s=60, clip_length_s=3.0)
    kinds = {s.kind for s in chosen}

    assert kinds == {"photo", "video"}
    assert sum(s.is_photo for s in chosen) < sum(not s.is_photo for s in chosen)


def test_with_no_video_the_film_can_be_all_stills() -> None:
    photos = [scene(asset=f"p{i}", kind="photo", month=i) for i in range(1, 7)]
    chosen = select_scenes(photos, target_length_s=30, clip_length_s=3.0)
    assert chosen and all(s.is_photo for s in chosen)


def test_an_old_cache_entry_without_a_kind_reads_as_video() -> None:
    """Caches written before stills existed must still load, not crash a resumed run."""
    row = scene().to_json()
    del row["kind"]
    assert Scene.from_json(row).kind == "video"


@pytestmark_ffmpeg
def test_cut_and_concat_produce_a_playable_film(clip: Path, tmp_path: Path) -> None:
    """The assembly, for real: two cuts joined into one file that ffprobe can read back."""
    from immich_addons.core.media import media_info

    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _cut(clip, a, 0.5, 3.5)
    _cut(clip, b, 3.5, 5.5)
    assert media_info(a).duration_s == pytest.approx(3.0, abs=0.6)

    film = tmp_path / "film.mp4"
    _concat([a, b], film, music=None)

    info = media_info(film)
    assert info.width == 1920 and info.height == 1080
    assert info.duration_s == pytest.approx(5.0, abs=1.0)
    assert not film.with_suffix(".txt").exists(), "the concat list should be cleaned up"


@pytestmark_ffmpeg
def test_a_month_card_renders_at_the_shared_geometry(tmp_path: Path) -> None:
    """Title cards are concatenated with the clips, so they must match frame for frame."""
    from immich_addons.addons.year_highlights.addon import font_file

    if font_file() is None:
        pytest.skip("no font available for drawtext")

    from immich_addons.core.media import media_info

    card = tmp_path / "july.mp4"
    _title_card(card, "July")

    info = media_info(card)
    assert info.width == 1920 and info.height == 1080
    assert info.duration_s == pytest.approx(1.5, abs=0.3)


@pytestmark_ffmpeg
def test_a_card_without_a_font_fails_loudly() -> None:
    """The addon downgrades to no cards, but only because this raises rather than rendering
    something blank."""
    import immich_addons.addons.year_highlights.addon as module

    original = module.FONT_CANDIDATES
    module.FONT_CANDIDATES = ("/definitely/not/a/font.ttf",)
    try:
        with pytest.raises(AddonError, match="font"):
            _title_card(Path("unused.mp4"), "July")
    finally:
        module.FONT_CANDIDATES = original


@pytestmark_ffmpeg
def test_a_still_becomes_a_clip_of_the_right_length(tmp_path: Path) -> None:
    from immich_addons.core.media import media_info

    photo = tmp_path / "p.jpg"
    _still_fixture(photo)

    out = tmp_path / "kb.mp4"
    _ken_burns(photo, out)

    info = media_info(out)
    assert info.width == 1920 and info.height == 1080
    assert info.duration_s == pytest.approx(2.0, abs=0.4)


@pytestmark_ffmpeg
def test_a_portrait_still_is_padded_not_stretched(tmp_path: Path) -> None:
    from immich_addons.core.media import media_info

    photo = tmp_path / "tall.jpg"
    _still_fixture(photo, size="600x1200")

    out = tmp_path / "kb.mp4"
    _ken_burns(photo, out)
    assert (media_info(out).width, media_info(out).height) == (1920, 1080)


@pytestmark_ffmpeg
def test_music_is_mixed_with_the_clip_audio_when_ducking(clip: Path, tmp_path: Path) -> None:
    """Ducked audio must still be audible on both sides — a filter typo here silences the film."""
    from immich_addons.core.media import media_info

    segment = tmp_path / "a.mp4"
    _cut(clip, segment, 0.5, 3.5)

    music = tmp_path / "music.m4a"
    _tone_fixture(music, seconds=1.0)  # deliberately shorter than the film, to test looping

    film = tmp_path / "film.mp4"
    _concat([segment, segment], film, music=music, duck=True)

    info = media_info(film)
    assert info.duration_s == pytest.approx(6.0, abs=1.0), "the short track should loop, not cut"
    assert audio_rms(film, 0.5, 3.0) > 0.0


def _still_fixture(dest: Path, *, size: str = "1200x800") -> None:
    subprocess.run(  # noqa: S603
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:duration=1:rate=1",
            "-frames:v",
            "1",
            str(dest),
        ],
        check=True,
    )


def _tone_fixture(dest: Path, *, seconds: float) -> None:
    subprocess.run(  # noqa: S603
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=True,
    )


# --- the job ---------------------------------------------------------------------------------


@pytest.fixture
def year_stack(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    videos = [
        {
            "id": f"v{m}",
            "type": "VIDEO",
            "originalFileName": f"C000{m}.MP4",
            "tags": [],
            "fileCreatedAt": f"2026-{m:02d}-05T12:00:00+00:00",
        }
        for m in (2, 6, 11)
    ]
    photos = [
        {
            "id": f"p{m}",
            "type": "IMAGE",
            "originalFileName": f"DSC0{m}.JPG",
            "tags": [],
            "fileCreatedAt": f"2026-{m:02d}-09T12:00:00+00:00",
        }
        for m in (2, 6, 11)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/search/metadata":
            import json as jsonlib

            body = jsonlib.loads(request.content)
            items = photos if body.get("type") == "IMAGE" else videos
            page = body["page"]
            return httpx.Response(200, json={"assets": {"items": items if page == 1 else []}})
        if path.endswith("/original"):
            return httpx.Response(200, content=b"not really a video")
        if path.endswith("/thumbnail"):
            return httpx.Response(200, content=b"not really a jpeg")
        return httpx.Response(200, json={})

    from immich_addons.addons.year_highlights import addon as module

    monkeypatch.setattr(
        module,
        "detect_scenes",
        lambda path, **kw: [(0.0, 5.0), (6.0, 11.0)],
    )
    monkeypatch.setattr(module, "analyse_scene", lambda path, s, e: (0.5, 0.5, [0.25] * 16))
    monkeypatch.setattr(module, "audio_rms", lambda path, s, e: 0.5)
    return httpx.MockTransport(handler), module


def _run(module, settings, config, jobs, monkeypatch, transport):  # noqa: ANN001, ANN202
    monkeypatch.setattr(
        module,
        "ImmichClient",
        lambda s, dry_run=False: ImmichClient(s, dry_run=dry_run, transport=transport),
    )
    addon = YearHighlights(settings)
    jobs.register("year-highlights", lambda ctx: addon.run(ctx, config))
    job_id = jobs.enqueue("year-highlights", {})
    jobs.run_job(job_id)
    return jobs.get(job_id)


def test_a_dry_run_selects_across_months_and_writes_nothing(
    settings,
    year_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    transport, module = year_stack
    settings.ensure_data_dirs()
    config = YearHighlightsConfig(year=2026, target_length_s=30, include_photos=False, dry_run=True)
    job = _run(module, settings, config, JobQueue(tmp_path / "j.sqlite"), monkeypatch, transport)

    assert job is not None and job.status is JobStatus.DONE, job.log
    assert "February, June, November" in job.log
    assert [a["kind"] for a in job.artifacts] == ["clip"] * len(job.artifacts)
    assert not (settings.output_dir / "highlights_2026.mp4").exists()


def test_photos_reach_the_film_when_they_are_asked_for(
    settings,
    year_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """`include_photos` was a switch that did nothing until Phase 7; it must now do something."""
    transport, module = year_stack
    settings.ensure_data_dirs()
    config = YearHighlightsConfig(year=2026, target_length_s=60, include_photos=True, dry_run=True)
    job = _run(module, settings, config, JobQueue(tmp_path / "j.sqlite"), monkeypatch, transport)

    assert job is not None and job.status is JobStatus.DONE, job.log
    kinds = [a["kind"] for a in job.artifacts]
    assert "still" in kinds, "a still should have been selected"
    assert "clip" in kinds, "and it should not have crowded out the video"


def test_photos_stay_a_minority_of_the_film(
    settings,
    year_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """A highlight film with photos is not a slideshow: the reserved share is a ceiling."""
    transport, module = year_stack
    settings.ensure_data_dirs()
    config = YearHighlightsConfig(year=2026, target_length_s=120, include_photos=True, dry_run=True)
    job = _run(module, settings, config, JobQueue(tmp_path / "j.sqlite"), monkeypatch, transport)

    assert job is not None
    kinds = [a["kind"] for a in job.artifacts]
    assert kinds.count("still") < kinds.count("clip")


def test_a_second_run_reuses_the_cached_analysis(
    settings,
    year_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """§6.4 acceptance: kill and rerun resumes from cache instead of restarting."""
    transport, module = year_stack
    settings.ensure_data_dirs()
    config = YearHighlightsConfig(year=2026, target_length_s=30, dry_run=True)

    _run(module, settings, config, JobQueue(tmp_path / "j1.sqlite"), monkeypatch, transport)
    second = _run(
        module, settings, config, JobQueue(tmp_path / "j2.sqlite"), monkeypatch, transport
    )

    assert second is not None and second.status is JobStatus.DONE
    assert "reused cached analysis for 3 video(s)" in second.log


def test_no_videos_is_a_readable_failure(settings, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from immich_addons.addons.year_highlights import addon as module

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"assets": {"items": []}})
    )
    settings.ensure_data_dirs()
    job = _run(
        module,
        settings,
        YearHighlightsConfig(year=2026, dry_run=True),
        JobQueue(tmp_path / "j.sqlite"),
        monkeypatch,
        transport,
    )
    assert job is not None and job.status is JobStatus.FAILED
    assert "no videos found for 2026" in job.log


def test_a_missing_music_file_is_a_readable_failure(settings) -> None:  # noqa: ANN001
    settings.ensure_data_dirs()
    addon = YearHighlights(settings)
    with pytest.raises(Exception, match="not found"):
        addon._music_path(YearHighlightsConfig(music_file="nope.mp3"))


def test_music_cannot_escape_the_music_directory(settings) -> None:  # noqa: ANN001
    settings.ensure_data_dirs()
    addon = YearHighlights(settings)
    with pytest.raises(Exception, match="not found"):
        addon._music_path(YearHighlightsConfig(music_file="../../../etc/passwd"))


def test_the_addon_is_marked_resumable() -> None:
    """The queue requeues an interrupted run rather than failing it — that is what makes the
    cache worth having."""
    assert YearHighlights.resumable is True


def test_originals_are_never_modified(settings, year_stack, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """The addon only ever downloads; the client cannot express a modify-original call at all."""
    transport, module = year_stack
    settings.ensure_data_dirs()
    writes: list[str] = []

    original = ImmichClient._request

    def spy(self, name, **kwargs):  # noqa: ANN001, ANN202
        from immich_addons.core.client import ENDPOINTS

        if ENDPOINTS[name].writes:
            writes.append(name)
        return original(self, name, **kwargs)

    monkeypatch.setattr(ImmichClient, "_request", spy)
    config = YearHighlightsConfig(year=2026, target_length_s=30, dry_run=True)
    _run(module, settings, config, JobQueue(tmp_path / "j.sqlite"), monkeypatch, transport)

    assert writes == [], "a dry run must not even attempt a write"
