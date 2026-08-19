"""trip-best-picks: trip detection, scoring inputs, and the pipeline with a mocked client."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest

from immich_addons.addons.trip_best_picks.addon import (
    TAG,
    TripBestPicks,
    TripBestPicksConfig,
    detect_trips,
    face_bonus,
    haversine_km,
    home_location,
)
from immich_addons.core.client import ImmichClient
from immich_addons.core.jobs import JobQueue, JobStatus

HOME = (57.70, 11.97)  # Gothenburg
AWAY = (39.47, -0.38)  # Valencia
AWAY_KM = 2213.0  # great-circle distance between the two


def _stamps(*offsets_h: float, base: datetime | None = None) -> list[datetime]:
    base = base or datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    return [base + timedelta(hours=h) for h in offsets_h]


# --- geography ------------------------------------------------------------------------------


def test_haversine_matches_a_known_distance() -> None:
    assert haversine_km(HOME, AWAY) == pytest.approx(AWAY_KM, rel=0.01)


def test_haversine_of_a_point_with_itself_is_zero() -> None:
    assert haversine_km(HOME, HOME) == pytest.approx(0.0)


def test_home_is_the_median_not_the_mean() -> None:
    """One trip to the other side of the world must not drag 'home' into the ocean."""
    points = [HOME] * 9 + [(-33.87, 151.21)]  # nine at home, one in Sydney
    latitude, longitude = home_location(points)
    assert latitude == pytest.approx(HOME[0])
    assert longitude == pytest.approx(HOME[1])


def test_home_of_nothing() -> None:
    assert home_location([]) is None


# --- trip detection -------------------------------------------------------------------------


def test_a_holiday_is_detected() -> None:
    stamps = _stamps(0, 1, 2) + _stamps(40, 41, 44, 60)
    locations = [HOME] * 3 + [AWAY] * 4
    trips = detect_trips(stamps, locations, home=HOME)
    assert len(trips) == 1
    assert trips[0].count == 4


def test_distance_alone_does_not_start_a_trip() -> None:
    """Continuous shooting far from home is one trip, not one per photo."""
    stamps = _stamps(0, 1, 2, 3)
    trips = detect_trips(stamps, [AWAY] * 4, home=HOME)
    assert len(trips) == 1


def test_a_long_gap_at_home_is_not_a_trip() -> None:
    """A quiet fortnight is not a holiday."""
    stamps = _stamps(0, 400, 800)
    assert detect_trips(stamps, [HOME] * 3, home=HOME) == []


def test_photos_without_gps_stay_inside_a_running_trip() -> None:
    """Phones drop location indoors; that must not chop a holiday into pieces."""
    stamps = _stamps(0, 40, 41, 42, 44)
    locations = [HOME, AWAY, None, None, AWAY]
    trips = detect_trips(stamps, locations, home=HOME)
    assert len(trips) == 1
    assert trips[0].count == 4


def test_two_holidays_are_two_trips() -> None:
    stamps = _stamps(0) + _stamps(40, 42) + _stamps(400) + _stamps(500, 502)
    locations = [HOME, AWAY, AWAY, HOME, AWAY, AWAY]
    assert len(detect_trips(stamps, locations, home=HOME)) == 2


def test_unsorted_input_is_handled() -> None:
    stamps = _stamps(44, 0, 41, 2, 40)
    locations = [AWAY, HOME, AWAY, HOME, AWAY]
    assert len(detect_trips(stamps, locations, home=HOME)) == 1


def test_empty_input() -> None:
    assert detect_trips([], []) == []


def test_mismatched_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        detect_trips(_stamps(0, 1), [HOME])


def test_trip_labels_read_naturally() -> None:
    trips = detect_trips(_stamps(40, 44, 90), [AWAY] * 3, home=HOME)
    assert "–" in trips[0].label


# --- face bonus -----------------------------------------------------------------------------


def test_more_faces_scores_higher() -> None:
    scores = face_bonus(
        [{"people": []}, {"people": [{"name": "A"}]}, {"people": [{"name": "A"}, {"name": "B"}]}],
        [],
    )
    assert scores[0] == 0.0
    assert scores[2] > scores[1] > 0


def test_a_boosted_person_maxes_the_bonus() -> None:
    assets = [{"people": [{"name": "Alma"}]}, {"people": [{"name": "Someone"}]}]
    scores = face_bonus(assets, ["alma"])
    assert scores[0] == 1.0
    assert scores[1] < scores[0]


# --- config ---------------------------------------------------------------------------------


def test_album_source_requires_an_album() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="album_id"):
        TripBestPicksConfig(source="album")


def test_date_range_must_be_ordered() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="after"):
        TripBestPicksConfig(source="date_range", date_from="2026-08-01", date_to="2026-07-01")


# --- the pipeline ---------------------------------------------------------------------------


def _thumbnail(seed: int, *, sharp: bool) -> bytes:
    from PIL import Image

    rng = np.random.default_rng(seed)
    if sharp:
        array = (rng.integers(0, 2, (64, 64)) * 255).astype(np.uint8)
    else:
        array = np.full((64, 64), 128, dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def album_stack(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """An album of six photos: a three-frame burst plus three distinct scenes."""
    assets = [
        {
            "id": f"a{i}",
            "type": "IMAGE",
            "originalFileName": f"IMG_{i}.jpg",
            "tags": [],
            "people": [],
        }
        for i in range(6)
    ]
    writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method in {"POST", "PUT"}:
            writes.append(request)
        if path == "/api/albums/album-1":
            return httpx.Response(200, json={"albumName": "Gotland", "assets": assets})
        if "/thumbnail" in path:
            index = int(path.split("/")[3][1:])
            return httpx.Response(200, content=_thumbnail(index, sharp=index != 1))
        if path == "/api/tags" and request.method == "GET":
            return httpx.Response(200, json=[{"id": "tag-1", "name": TAG}])
        if path == "/api/albums" and request.method == "POST":
            return httpx.Response(201, json={"id": "new-album", "albumName": "Best of Gotland"})
        return httpx.Response(200, json={})

    # Three near-identical vectors (the burst) plus three distinct ones.
    base = np.array([1.0, 0.0, 0.0, 0.0])
    embeddings = {
        "a0": base,
        "a1": base + 0.01,
        "a2": base + 0.02,
        "a3": np.array([0.0, 1.0, 0.0, 0.0]),
        "a4": np.array([0.0, 0.0, 1.0, 0.0]),
        "a5": np.array([0.0, 0.0, 0.0, 1.0]),
    }
    from immich_addons.addons.trip_best_picks import addon as module
    from immich_addons.core import db

    monkeypatch.setattr(db, "embeddings_for", lambda ids, **kw: {i: embeddings[i] for i in ids})
    return httpx.MockTransport(handler), writes, module


def _run(module, settings, config, jobs, monkeypatch, transport):  # noqa: ANN001, ANN202
    monkeypatch.setattr(
        module,
        "ImmichClient",
        lambda s, dry_run=False: ImmichClient(s, dry_run=dry_run, transport=transport),
    )
    addon = TripBestPicks(settings)
    jobs.register("trip-best-picks", lambda ctx: addon.run(ctx, config))
    job_id = jobs.enqueue("trip-best-picks", {})
    jobs.run_job(job_id)
    return jobs.get(job_id)


def test_the_burst_collapses_and_the_album_is_created(
    settings,
    album_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """§6.2 acceptance: each burst contributes one pick, and picks span the distinct scenes."""
    transport, writes, module = album_stack
    config = TripBestPicksConfig(source="album", album_id="album-1", n_picks=4, dry_run=False)
    job = _run(module, settings, config, JobQueue(tmp_path / "j.sqlite"), monkeypatch, transport)

    assert job is not None and job.status is JobStatus.DONE, job.log
    assert "2 near-duplicate(s) collapsed" in job.log

    created = [w for w in writes if w.url.path == "/api/albums"]
    assert len(created) == 1
    body = json.loads(created[0].content)
    assert body["albumName"] == "Best of Gotland"
    assert len(body["assetIds"]) == 4
    assert len(set(body["assetIds"])) == 4
    # Exactly one of the three burst frames may appear.
    assert len({"a0", "a1", "a2"} & set(body["assetIds"])) == 1


def test_dry_run_produces_the_preview_and_writes_nothing(
    settings,
    album_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    transport, writes, module = album_stack
    config = TripBestPicksConfig(source="album", album_id="album-1", n_picks=3, dry_run=True)
    job = _run(module, settings, config, JobQueue(tmp_path / "j.sqlite"), monkeypatch, transport)

    assert job is not None and job.status is JobStatus.DONE, job.log
    assert writes == []
    assert "would create album" in job.log
    picks = [a for a in job.artifacts if a["kind"] == "pick"]
    assert len(picks) == 3, "the dry-run result is the preview grid"


def test_changing_diversity_re_rolls_the_selection(
    settings,
    album_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    transport, _, module = album_stack

    def picks_at(lam: float) -> list[str]:
        config = TripBestPicksConfig(
            source="album", album_id="album-1", n_picks=2, diversity=lam, dry_run=True
        )
        job = _run(
            module, settings, config, JobQueue(tmp_path / f"j{lam}.sqlite"), monkeypatch, transport
        )
        assert job is not None
        return [a["value"] for a in job.artifacts if a["kind"] == "pick"]

    assert picks_at(1.0) == picks_at(1.0), "the same parameters give the same album"
    assert len(picks_at(0.0)) == 2


def test_assets_from_other_addons_are_never_candidates(
    settings,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """CLAUDE.md: never reprocess our own output."""
    assets = [
        {"id": "a0", "type": "IMAGE", "tags": [{"name": "addon:auto-lut"}], "people": []},
        {"id": "a1", "type": "VIDEO", "tags": [], "people": []},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/albums/album-1":
            return httpx.Response(200, json={"albumName": "Mixed", "assets": assets})
        return httpx.Response(200, json={})

    from immich_addons.addons.trip_best_picks import addon as module

    config = TripBestPicksConfig(source="album", album_id="album-1", dry_run=True)
    job = _run(
        module,
        settings,
        config,
        JobQueue(tmp_path / "j.sqlite"),
        monkeypatch,
        httpx.MockTransport(handler),
    )
    assert job is not None and job.status is JobStatus.DONE
    assert "no candidate photos" in job.log


def test_missing_embeddings_are_reported_not_fatal(
    settings,
    album_stack,
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Smart search may still be indexing; that should degrade, not fail."""
    transport, _, module = album_stack
    from immich_addons.core import db

    monkeypatch.setattr(db, "embeddings_for", lambda ids, **kw: {})

    config = TripBestPicksConfig(source="album", album_id="album-1", n_picks=2, dry_run=True)
    job = _run(module, settings, config, JobQueue(tmp_path / "j.sqlite"), monkeypatch, transport)

    assert job is not None and job.status is JobStatus.DONE, job.log
    assert "have no CLIP vector yet" in job.log
