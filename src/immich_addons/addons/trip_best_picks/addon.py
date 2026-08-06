"""trip-best-picks — the best *and most varied* photos from a trip, into an album (PLAN.md §6.2).

The selection is four steps, each a pure function from :mod:`immich_addons.core.scoring`:

1. score each candidate (sharpness, exposure, faces);
2. collapse near-duplicates, so a five-frame burst spends one slot rather than five;
3. MMR-select ``n_picks`` from what is left, trading quality against variety;
4. create the album.

Embeddings come either from Immich's own CLIP vectors (``db``, fast) or are computed locally
(``local``, slower, no database access). Everything before step 4 is read-only, so a dry run is the
whole pipeline minus the album.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

import numpy as np
from pydantic import Field, model_validator

from immich_addons.addons.base import Addon, AddonConfig, AddonError
from immich_addons.core import scoring
from immich_addons.core.client import ImmichClient
from immich_addons.core.config import Settings, get_settings
from immich_addons.core.jobs import JobContext

log = logging.getLogger(__name__)

ADDON_ID = "trip-best-picks"
TAG = f"addon:{ADDON_ID}"

#: A new trip starts on a gap this long *combined with* distance from home (PLAN.md §6.2).
TRIP_GAP = timedelta(hours=18)
TRIP_DISTANCE_KM = 150.0
EARTH_RADIUS_KM = 6371.0


class TripBestPicksConfig(AddonConfig):
    source: str = Field(
        default="trip",
        title="Source",
        description="Where the candidate photos come from.",
        json_schema_extra={"enum": ["album", "date_range", "trip"]},
    )
    album_id: str = Field(default="", title="Album", json_schema_extra={"x-picker": "albums"})
    date_from: date | None = Field(default=None, title="From")
    date_to: date | None = Field(default=None, title="To")
    n_picks: int = Field(default=24, ge=1, le=500, title="Number of picks")
    people_boost: list[str] = Field(
        default_factory=list,
        title="Favour these people",
        description="Photos containing them score higher.",
        json_schema_extra={"x-picker": "people"},
    )
    embedding_source: str = Field(
        default="db",
        title="Embeddings from",
        description=(
            "db reads Immich's CLIP vectors (fast, needs the read-only Postgres role); "
            "local computes them on CPU (slower, no database access)."
        ),
        json_schema_extra={"enum": ["db", "local"]},
    )
    diversity: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        title="Quality vs variety",
        description="MMR lambda. 1.0 picks purely on quality; lower values spread the selection.",
    )
    near_dup_threshold: float = Field(
        default=0.96,
        ge=0.5,
        le=1.0,
        title="Burst threshold",
        description="Cosine similarity above which two photos count as the same moment.",
    )
    album_name: str = Field(
        default="",
        title="Album name",
        description="Defaults to 'Best of {trip}'.",
    )

    @model_validator(mode="after")
    def _check_source_fields(self) -> TripBestPicksConfig:
        if self.source not in {"album", "date_range", "trip"}:
            raise ValueError("source must be one of album, date_range, trip")
        if self.embedding_source not in {"db", "local"}:
            raise ValueError("embedding_source must be db or local")
        if self.source == "album" and not self.album_id:
            raise ValueError("source 'album' needs an album_id")
        if self.source == "date_range" and not (self.date_from and self.date_to):
            raise ValueError("source 'date_range' needs both date_from and date_to")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        return self


@dataclass(frozen=True)
class Trip:
    """A detected trip: a run of photos away from home."""

    start: datetime
    end: datetime
    count: int

    @property
    def label(self) -> str:
        if self.start.date() == self.end.date():
            return self.start.strftime("%d %b %Y")
        if self.start.year == self.end.year:
            return f"{self.start:%d %b}–{self.end:%d %b %Y}"
        return f"{self.start:%d %b %Y}–{self.end:%d %b %Y}"


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (latitude, longitude) pairs, in kilometres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, h)))


def home_location(points: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """Median latitude and longitude — where most photos are taken, i.e. home.

    The median rather than the mean, so a single trip to the other side of the world does not drag
    "home" into the ocean.
    """
    usable = [p for p in points if p is not None]
    if not usable:
        return None
    lats = sorted(p[0] for p in usable)
    lons = sorted(p[1] for p in usable)
    mid = len(usable) // 2
    return lats[mid], lons[mid]


def detect_trips(
    stamps: Sequence[datetime],
    locations: Sequence[tuple[float, float] | None],
    *,
    gap: timedelta = TRIP_GAP,
    distance_km: float = TRIP_DISTANCE_KM,
    home: tuple[float, float] | None = None,
) -> list[Trip]:
    """Find trips in a (timestamp, GPS) stream.

    A trip is a run of photos that are **both** far from home and separated from the previous run
    by a long gap. Both conditions matter: a long gap alone is just a quiet week, and distance
    alone would split a single holiday every time you went to sleep.

    Photos without GPS are kept inside a run once it has started (phones drop location indoors),
    but cannot start one.
    """
    if len(stamps) != len(locations):
        raise ValueError("stamps and locations must be the same length")
    order = sorted(range(len(stamps)), key=lambda i: stamps[i])
    if not order:
        return []

    if home is None:
        home = home_location([loc for loc in locations if loc is not None])

    trips: list[Trip] = []
    start: datetime | None = None
    end: datetime | None = None
    count = 0
    previous: datetime | None = None

    def away(index: int) -> bool:
        location = locations[index]
        if location is None or home is None:
            return False
        return haversine_km(location, home) > distance_km

    for index in order:
        stamp = stamps[index]
        far = away(index)
        broke = previous is None or (stamp - previous) > gap

        if start is None:
            if far and broke:
                start, end, count = stamp, stamp, 1
        elif broke and not far:
            trips.append(Trip(start, end or start, count))
            start = end = None
            count = 0
        else:
            end = stamp
            count += 1

        previous = stamp

    if start is not None:
        trips.append(Trip(start, end or start, count))
    return trips


def face_bonus(assets: Sequence[dict[str, Any]], boosted: Sequence[str]) -> np.ndarray:
    """0..1 per asset: more faces is better, and a boosted person is better still."""
    wanted = {name.strip().lower() for name in boosted if name.strip()}
    out = np.zeros(len(assets), dtype=np.float64)
    for i, asset in enumerate(assets):
        people = asset.get("people") or []
        if not people:
            continue
        score = min(1.0, 0.4 + 0.2 * len(people))
        names = {
            str(p.get("name") or p.get("id") or "").lower() for p in people if isinstance(p, dict)
        }
        if wanted and (names & wanted):
            score = 1.0
        elif wanted:
            score *= 0.5
        out[i] = score
    return out


class TripBestPicks(Addon):
    id: ClassVar[str] = ADDON_ID
    name: ClassVar[str] = "Trip Best Picks"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Selects the best photos from a trip while keeping the selection varied, "
        "and collects them in an album."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("manual",)
    config_model: ClassVar[type[AddonConfig]] = TripBestPicksConfig

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        assert isinstance(config, TripBestPicksConfig)

        with ImmichClient(self.settings, dry_run=config.dry_run) as client:
            ctx.progress(0.05, "gathering candidates")
            assets, label = self._candidates(ctx, client, config)
            if not assets:
                ctx.log("no candidate photos for that source")
                return
            ctx.log(f"{len(assets)} candidates from {label}")

            ctx.progress(0.25, "reading embeddings")
            embeddings = self._embeddings(ctx, client, config, assets)

            ctx.progress(0.55, "scoring")
            quality = self._quality(ctx, client, config, assets)

            ctx.progress(0.75, "selecting")
            kept = scoring.collapse_bursts(embeddings, quality, config.near_dup_threshold)
            ctx.log(f"{len(assets) - len(kept)} near-duplicate(s) collapsed")

            chosen_local = scoring.mmr_select(
                embeddings[kept], quality[kept], config.n_picks, lam=config.diversity
            )
            picked = [assets[kept[i]] for i in chosen_local]
            ctx.log(f"selected {len(picked)} of {len(kept)}")

            album_name = config.album_name or f"Best of {label}"
            self._publish(ctx, client, config, picked, album_name)

    # --- steps ---------------------------------------------------------------------------

    def _candidates(
        self, ctx: JobContext, client: ImmichClient, config: TripBestPicksConfig
    ) -> tuple[list[dict[str, Any]], str]:
        if config.source == "album":
            album = client.album_info(config.album_id)
            assets = [a for a in album.get("assets", []) if isinstance(a, dict)]
            return self._photos_only(assets), str(album.get("albumName") or config.album_id)

        if config.source == "date_range":
            assert config.date_from and config.date_to
            assets = list(
                client.iter_metadata(
                    taken_after=datetime.combine(config.date_from, datetime.min.time()),
                    taken_before=datetime.combine(config.date_to, datetime.max.time()),
                    asset_type="IMAGE",
                )
            )
            return self._photos_only(assets), f"{config.date_from} – {config.date_to}"

        trips, assets = self.detect_trips_from_library(client)
        if not trips:
            raise AddonError(
                "no trips detected — the library needs photos with GPS taken more than "
                f"{TRIP_DISTANCE_KM:.0f} km from home. Use the album or date-range source instead."
            )
        trip = max(trips, key=lambda t: t.count)
        ctx.log(f"{len(trips)} trip(s) detected; using the largest: {trip.label} ({trip.count})")
        in_trip = [
            a
            for a in assets
            if (stamp := _taken_at(a)) is not None and trip.start <= stamp <= trip.end
        ]
        return self._photos_only(in_trip), trip.label

    def detect_trips_from_library(
        self, client: ImmichClient, *, limit: int = 20000
    ) -> tuple[list[Trip], list[dict[str, Any]]]:
        """Scan the library's (timestamp, GPS) stream. Also used by the UI to list trips."""
        assets: list[dict[str, Any]] = []
        for asset in client.iter_metadata(asset_type="IMAGE"):
            assets.append(asset)
            if len(assets) >= limit:
                break

        stamps: list[datetime] = []
        locations: list[tuple[float, float] | None] = []
        usable: list[dict[str, Any]] = []
        for asset in assets:
            stamp = _taken_at(asset)
            if stamp is None:
                continue
            usable.append(asset)
            stamps.append(stamp)
            locations.append(_location(asset))

        return detect_trips(stamps, locations), usable

    def _embeddings(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: TripBestPicksConfig,
        assets: list[dict[str, Any]],
    ) -> np.ndarray:
        ids = [str(a["id"]) for a in assets]

        if config.embedding_source == "db":
            from immich_addons.core import db

            vectors = db.embeddings_for(ids, settings=self.settings)
            missing = [i for i in ids if i not in vectors]
            if missing:
                ctx.log(
                    f"{len(missing)} asset(s) have no CLIP vector yet "
                    "(smart search may still be indexing); they are scored as neutral"
                )
            width = len(next(iter(vectors.values()))) if vectors else 512
            return np.stack(
                [vectors.get(i, np.zeros(width, dtype=np.float32)) for i in ids]
            ).astype(np.float64)

        return self._local_embeddings(ctx, client, assets)

    def _local_embeddings(
        self, ctx: JobContext, client: ImmichClient, assets: list[dict[str, Any]]
    ) -> np.ndarray:
        """CLIP on the CPU, over thumbnails. Optional dependency, so fail with a clear message."""
        try:
            import open_clip  # type: ignore[import-not-found]
            import torch  # type: ignore[import-not-found]
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - depends on an optional extra
            raise AddonError(
                "embedding_source 'local' needs the optional extra: "
                "`uv sync --extra local-clip`. Use 'db' to read Immich's own vectors instead."
            ) from exc

        import io

        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        model.eval()

        vectors = []
        for i, asset in enumerate(assets):
            if i % 25 == 0:
                ctx.progress(0.25 + 0.3 * i / max(1, len(assets)), f"embedding {i}/{len(assets)}")
            raw = client.thumbnail(str(asset["id"]))
            with Image.open(io.BytesIO(raw)) as image:
                tensor = preprocess(image.convert("RGB")).unsqueeze(0)
            with torch.no_grad():
                vectors.append(model.encode_image(tensor)[0].numpy())
        return np.stack(vectors).astype(np.float64)

    def _quality(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: TripBestPicksConfig,
        assets: list[dict[str, Any]],
    ) -> np.ndarray:
        import io

        from PIL import Image

        sharp: list[float] = []
        exposed: list[float] = []
        for i, asset in enumerate(assets):
            if i % 25 == 0:
                ctx.progress(0.55 + 0.2 * i / max(1, len(assets)), f"scoring {i}/{len(assets)}")
            try:
                raw = client.thumbnail(str(asset["id"]), size="thumbnail")
                with Image.open(io.BytesIO(raw)) as image:
                    array = np.asarray(image.convert("L"), dtype=np.float64)
            except Exception:  # noqa: BLE001 - one unreadable thumbnail must not fail the run
                log.warning("could not read a thumbnail for %s", asset.get("id"))
                sharp.append(0.0)
                exposed.append(0.5)
                continue
            sharp.append(scoring.sharpness(array))
            exposed.append(scoring.exposure(array))

        return scoring.quality_scores(sharp, exposed, face_bonus(assets, config.people_boost))

    def _publish(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: TripBestPicksConfig,
        picked: list[dict[str, Any]],
        album_name: str,
    ) -> None:
        ids = [str(a["id"]) for a in picked]
        for asset in picked:
            ctx.artifact("pick", str(asset["id"]), str(asset.get("originalFileName") or ""))

        if config.dry_run:
            # The dry-run result *is* the preview grid: the job's artifacts are the picks.
            ctx.log(f"dry run: would create album {album_name!r} with {len(ids)} photos")
            ctx.log("re-run with a different 'Quality vs variety' to re-roll the selection")
            return

        album = client.create_album(album_name, asset_ids=ids)
        album_id = str(album.get("id") or "")
        ctx.artifact("album", album_id, album_name)
        ctx.log(f"created album {album_name!r} ({album_id}) with {len(ids)} photos")

        tag_id = _ensure_tag(client)
        if tag_id:
            client.assign_tag(tag_id, ids)
            ctx.log(f"tagged {len(ids)} photos {TAG}")

    @staticmethod
    def _photos_only(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Skip videos and anything another addon produced (CLAUDE.md: never reprocess our own)."""
        out = []
        for asset in assets:
            if str(asset.get("type", "IMAGE")).upper() != "IMAGE":
                continue
            tags = asset.get("tags") or []
            if any(str((t or {}).get("name", "")).startswith("addon:") for t in tags):
                continue
            out.append(asset)
        return out


def _ensure_tag(client: ImmichClient) -> str:
    for tag in client.tags():
        if str(tag.get("name") or "") == TAG:
            return str(tag.get("id") or "")
    return str(client.create_tag(TAG).get("id") or "")


def _taken_at(asset: dict[str, Any]) -> datetime | None:
    raw = (
        asset.get("fileCreatedAt")
        or asset.get("localDateTime")
        or (asset.get("exifInfo") or {}).get("dateTimeOriginal")
    )
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _location(asset: dict[str, Any]) -> tuple[float, float] | None:
    exif = asset.get("exifInfo") or {}
    lat, lon = exif.get("latitude"), exif.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None
