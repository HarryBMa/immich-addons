"""auto-lut: the idempotency guards, the event parser, and the pipeline with a mocked client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from immich_addons.addons.auto_lut.addon import (
    TAG,
    AutoLut,
    AutoLutConfig,
    State,
    asset_ids_from_event,
    config_hash,
    graded_filename,
    is_our_upload,
    should_process,
)
from immich_addons.core.client import ImmichClient
from immich_addons.core.jobs import JobQueue, JobStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_PAYLOAD = REPO_ROOT / "tests" / "fixtures" / "webhook_asset_create.json"

CONFIG = AutoLutConfig(lut="warm-film.cube", dry_run=False)


def asset(**overrides: object) -> dict:
    base = {
        "id": "asset-1",
        "type": "IMAGE",
        "originalFileName": "DSC07421.JPG",
        "tags": [],
        "exifInfo": {"model": "ILCE-6400"},
    }
    base.update(overrides)
    return base


# --- the four guards ------------------------------------------------------------------------


def test_a_normal_photo_is_processed() -> None:
    assert should_process(asset(), CONFIG) is None


def test_guard_1_an_addon_tagged_asset_is_skipped() -> None:
    skip = should_process(asset(tags=[{"name": "addon:auto-lut"}]), CONFIG)
    assert skip is not None
    assert "addon:*" in skip.reason


def test_guard_1_covers_other_addons_tags_too() -> None:
    """Anything another addon produced is off limits as well, not only our own output."""
    assert should_process(asset(tags=[{"name": "addon:zine-maker"}]), CONFIG) is not None


def test_guard_2_our_own_filename_pattern_is_skipped() -> None:
    name = graded_filename("asset-1", CONFIG)
    skip = should_process(asset(originalFileName=name), CONFIG)
    assert skip is not None
    assert "filename pattern" in skip.reason


def test_guard_3_an_already_graded_source_is_skipped() -> None:
    skip = should_process(asset(), CONFIG, already_done={"asset-1"})
    assert skip is not None
    assert "already graded" in skip.reason


def test_regrading_after_changing_the_look_is_allowed(tmp_path: Path) -> None:
    """A different LUT is a different output file, so it is not a duplicate.

    Goes through State the way the addon does: the done-set is computed per look, so switching
    LUTs presents an empty set and the asset is eligible again.
    """
    other = AutoLutConfig(lut="teal-orange.cube", dry_run=False)
    assert config_hash(other) != config_hash(CONFIG)

    state = State(tmp_path / "state.json")
    state.record("asset-1", CONFIG, "graded-1", graded_filename("asset-1", CONFIG))

    assert should_process(asset(), CONFIG, already_done=state.done_ids(CONFIG)) is not None
    assert should_process(asset(), other, already_done=state.done_ids(other)) is None


@pytest.mark.parametrize("name", ["auto-lut_asset-1_deadbeef.jpg", "AUTO-LUT_abc123_0f1e2d3c.JPG"])
def test_upload_pattern_recognises_our_files(name: str) -> None:
    assert is_our_upload(name) is True


@pytest.mark.parametrize("name", ["DSC07421.JPG", "auto-lut-holiday.jpg", "IMG_auto-lut_1.jpg"])
def test_upload_pattern_does_not_over_match(name: str) -> None:
    assert is_our_upload(name) is False


# --- scope ---------------------------------------------------------------------------------


def test_raw_is_skipped_with_a_reason() -> None:
    skip = should_process(asset(originalFileName="DSC07421.ARW"), CONFIG)
    assert skip is not None
    assert "RAW" in skip.reason


def test_videos_are_skipped_unless_enabled() -> None:
    clip = asset(type="VIDEO", originalFileName="C0001.MP4")
    assert should_process(clip, CONFIG) is not None
    assert should_process(clip, CONFIG.model_copy(update={"process_videos": True})) is None


def test_extensions_outside_the_scope_are_skipped() -> None:
    skip = should_process(asset(originalFileName="scan.png"), CONFIG)
    assert skip is not None
    assert "outside the configured scope" in skip.reason


def test_camera_model_scope() -> None:
    config = CONFIG.model_copy(update={"camera_models": ["ILCE-6400"]})
    assert should_process(asset(), config) is None
    assert should_process(asset(exifInfo={"model": "iPhone 15"}), config) is not None


def test_album_scope() -> None:
    config = CONFIG.model_copy(update={"album_ids": ["album-1"]})
    assert should_process(asset(), config, album_asset_ids={"asset-1"}) is None
    assert should_process(asset(), config, album_asset_ids={"other"}) is not None


def test_album_scope_fails_closed_when_the_album_cannot_be_read() -> None:
    """Unknown membership must mean "skip", never "process everything"."""
    config = CONFIG.model_copy(update={"album_ids": ["album-1"]})
    skip = should_process(asset(), config, album_asset_ids=None)
    assert skip is not None
    assert "could not be read" in skip.reason


# --- webhook payload parsing ----------------------------------------------------------------


def test_the_committed_payload_parses() -> None:
    payload = json.loads(WEBHOOK_PAYLOAD.read_text(encoding="utf-8"))
    assert asset_ids_from_event(payload) == ["5b6b0e6c-6d5e-4a6f-9a2c-7f1e2d3c4b5a"]


@pytest.mark.parametrize(
    "payload",
    [
        {"asset": {"id": "x"}},
        {"assetId": "x"},
        {"data": {"asset": {"id": "x"}}},
        {"items": [{"asset": {"id": "x"}}]},
        {"asset_id": "x"},
        {"id": "x", "type": "IMAGE", "originalFileName": "a.jpg"},
    ],
)
def test_parser_accepts_the_shapes_workflows_might_send(payload: dict) -> None:
    assert asset_ids_from_event(payload) == ["x"]


def test_parser_de_duplicates() -> None:
    assert asset_ids_from_event({"assetId": "x", "asset": {"id": "x"}}) == ["x"]


def test_an_unrecognised_payload_is_an_empty_list_not_a_crash() -> None:
    assert asset_ids_from_event({"hello": "world"}) == []
    assert asset_ids_from_event({}) == []


def test_parser_does_not_recurse_forever() -> None:
    payload: dict = {}
    node = payload
    for _ in range(50):
        node["nested"] = {}
        node = node["nested"]
    assert asset_ids_from_event(payload) == []


def test_on_event_returns_job_params() -> None:
    assert AutoLut().on_event({"assetId": "x"}, CONFIG) == [{"asset_id": "x"}]


# --- state ---------------------------------------------------------------------------------


def test_state_round_trips(tmp_path: Path) -> None:
    state = State(tmp_path / "auto_lut_state.json")
    state.record("asset-1", CONFIG, "graded-1", "auto-lut_asset-1_x.jpg")
    state.cursor = "2026-08-06T09:00:00+00:00"
    state.save()

    reloaded = State(tmp_path / "auto_lut_state.json")
    assert reloaded.done_ids(CONFIG) == {"asset-1"}
    assert reloaded.cursor == "2026-08-06T09:00:00+00:00"


def test_state_is_per_look(tmp_path: Path) -> None:
    state = State(tmp_path / "s.json")
    state.record("asset-1", CONFIG, "graded-1", "f.jpg")
    other = AutoLutConfig(lut="teal-orange.cube", dry_run=False)
    assert state.done_ids(other) == set()


def test_corrupt_state_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{broken", encoding="utf-8")
    assert State(path).done_ids(CONFIG) == set()


# --- the pipeline, with the network and ffmpeg mocked ----------------------------------------


@pytest.fixture
def lut_dir(settings) -> Path:  # noqa: ANN001
    settings.ensure_data_dirs()
    (settings.luts_dir / "warm-film.cube").write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    return settings.luts_dir


@pytest.fixture
def fake_immich(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """A transport that answers the endpoints the pipeline uses, and records the writes."""
    writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method in {"POST", "PUT"} and not path.startswith("/api/search"):
            writes.append(request)

        if path == "/api/assets/asset-1":
            return httpx.Response(200, json=asset())
        if path.endswith("/original"):
            return httpx.Response(200, content=b"original-bytes")
        if path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/api/tags" and request.method == "POST":
            return httpx.Response(201, json={"id": "tag-1", "name": TAG})
        if path == "/api/assets" and request.method == "POST":
            return httpx.Response(201, json={"id": "graded-1", "status": "created"})
        if path == "/api/stacks":
            return httpx.Response(201, json={"id": "stack-1"})
        if path.startswith("/api/tags/") and request.method == "PUT":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/search/metadata":
            return httpx.Response(200, json={"assets": {"items": [asset()]}})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler), writes


@pytest.fixture
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    """Replace the ffmpeg and exiftool calls; this suite tests the pipeline, not the encoder."""
    calls: list[tuple[Path, Path]] = []

    def fake_apply_lut(src: Path, dest: Path, lut: Path, **kwargs: object) -> Path:
        calls.append((src, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"graded-bytes")
        return dest

    from immich_addons.addons.auto_lut import addon as module

    monkeypatch.setattr(module.media, "apply_lut", fake_apply_lut)
    monkeypatch.setattr(module.media, "copy_exif", lambda src, dest: True)
    return calls


def _run(  # noqa: ANN202
    addon: AutoLut,
    config: AutoLutConfig,
    params: dict,
    jobs: JobQueue,
    monkeypatch,  # noqa: ANN001
    transport,  # noqa: ANN001
):
    """Run the addon once through a real JobQueue, with the client's transport injected."""
    from immich_addons.addons.auto_lut import addon as module

    # Take the real class from its own module: monkeypatching twice in one test would otherwise
    # wrap the previous patch and blow up on the injected keyword.
    monkeypatch.setattr(
        module,
        "ImmichClient",
        lambda settings, dry_run=False: ImmichClient(
            settings, dry_run=dry_run, transport=transport
        ),
    )
    jobs.register("auto-lut", lambda ctx: addon.run(ctx, config))
    job_id = jobs.enqueue("auto-lut", params)
    jobs.run_job(job_id)
    return jobs.get(job_id)


def test_a_webhook_event_produces_one_graded_stacked_tagged_copy(
    settings,
    lut_dir,
    fake_immich,
    no_ffmpeg,
    tmp_path,
    monkeypatch,  # noqa: ANN001
) -> None:
    transport, writes = fake_immich
    jobs = JobQueue(tmp_path / "jobs.sqlite")
    addon = AutoLut(settings)

    job = _run(
        addon,
        CONFIG,
        {"trigger": "webhook", "event": {"asset": {"id": "asset-1"}}},
        jobs,
        monkeypatch,
        transport,
    )

    assert job is not None and job.status is JobStatus.DONE, job.log
    paths = [f"{w.method} {w.url.path}" for w in writes]
    assert paths == [
        "POST /api/assets",
        "POST /api/tags",
        "PUT /api/tags/tag-1/assets",
        "POST /api/stacks",
    ]
    assert job.artifacts == [
        {"kind": "asset", "value": "graded-1", "label": "DSC07421.JPG graded with warm-film.cube"}
    ]


def test_re_sending_the_same_event_is_a_no_op(
    settings,
    lut_dir,
    fake_immich,
    no_ffmpeg,
    tmp_path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """PLAN.md §6.1 acceptance: idempotent."""
    transport, writes = fake_immich
    jobs = JobQueue(tmp_path / "jobs.sqlite")
    addon = AutoLut(settings)
    params = {"trigger": "webhook", "event": {"asset": {"id": "asset-1"}}}

    _run(addon, CONFIG, params, jobs, monkeypatch, transport)
    first = len(writes)
    second_job = _run(addon, CONFIG, params, jobs, monkeypatch, transport)

    assert len(writes) == first, "the second run wrote to Immich again"
    assert second_job is not None
    assert "already graded" in second_job.log


def test_the_graded_copy_cannot_re_trigger_the_addon(
    settings,
    lut_dir,
    no_ffmpeg,
    tmp_path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Feed the addon its own output and confirm nothing is written."""
    graded = asset(
        id="graded-1",
        originalFileName=graded_filename("asset-1", CONFIG),
        tags=[{"name": TAG}],
    )
    writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method in {"POST", "PUT"}:
            writes.append(request)
        if request.url.path == "/api/assets/graded-1":
            return httpx.Response(200, json=graded)
        return httpx.Response(200, json={})

    jobs = JobQueue(tmp_path / "jobs.sqlite")
    job = _run(
        AutoLut(settings),
        CONFIG,
        {"trigger": "webhook", "event": {"asset": {"id": "graded-1"}}},
        jobs,
        monkeypatch,
        httpx.MockTransport(handler),
    )

    assert job is not None and job.status is JobStatus.DONE
    assert writes == []
    assert "addon:*" in job.log


def test_dry_run_logs_every_intended_write_and_sends_none(
    settings,
    lut_dir,
    fake_immich,
    no_ffmpeg,
    tmp_path,
    monkeypatch,  # noqa: ANN001
) -> None:
    transport, writes = fake_immich
    jobs = JobQueue(tmp_path / "jobs.sqlite")
    config = CONFIG.model_copy(update={"dry_run": True})

    job = _run(
        AutoLut(settings),
        config,
        {"trigger": "webhook", "event": {"asset": {"id": "asset-1"}}},
        jobs,
        monkeypatch,
        transport,
    )

    assert job is not None and job.status is JobStatus.DONE, job.log
    assert writes == [], "a dry run reached the server"
    assert "would upload" in job.log
    assert "dry run: 1 write(s) were logged" in job.log


def test_poll_mode_advances_its_cursor(
    settings,
    lut_dir,
    no_ffmpeg,
    tmp_path,
    monkeypatch,  # noqa: ANN001
) -> None:
    page = [asset(fileCreatedAt="2026-08-06T09:40:58+00:00")]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/search/metadata":
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"assets": {"items": page if body["page"] == 1 else []}}
            )
        if path.endswith("/original"):
            return httpx.Response(200, content=b"original")
        if path == "/api/tags":
            return httpx.Response(200, json=[{"id": "tag-1", "name": TAG}])
        if path == "/api/assets":
            return httpx.Response(201, json={"id": "graded-1"})
        return httpx.Response(200, json={})

    jobs = JobQueue(tmp_path / "jobs.sqlite")
    job = _run(
        AutoLut(settings),
        CONFIG,
        {"trigger": "poll"},
        jobs,
        monkeypatch,
        httpx.MockTransport(handler),
    )

    assert job is not None and job.status is JobStatus.DONE, job.log
    state = State(settings.cache_dir / "auto_lut_state.json")
    assert state.cursor == "2026-08-06T09:40:58+00:00"


def test_a_missing_lut_fails_with_a_readable_message(
    settings,
    fake_immich,
    tmp_path,
    monkeypatch,  # noqa: ANN001
) -> None:
    transport, _ = fake_immich
    settings.ensure_data_dirs()
    jobs = JobQueue(tmp_path / "jobs.sqlite")
    job = _run(
        AutoLut(settings),
        AutoLutConfig(lut="nope.cube", dry_run=False),
        {"trigger": "poll"},
        jobs,
        monkeypatch,
        transport,
    )
    assert job is not None and job.status is JobStatus.FAILED
    assert "not found" in job.log


def test_a_lut_path_cannot_escape_the_lut_directory(settings, lut_dir) -> None:  # noqa: ANN001
    addon = AutoLut(settings)
    config = AutoLutConfig(lut="../../../etc/passwd", dry_run=False)
    with pytest.raises(Exception, match="not found"):
        addon._lut_path(config)


def test_no_lut_selected_is_a_clear_error(settings) -> None:  # noqa: ANN001
    with pytest.raises(Exception, match="no LUT selected"):
        AutoLut(settings)._lut_path(AutoLutConfig(dry_run=False))
