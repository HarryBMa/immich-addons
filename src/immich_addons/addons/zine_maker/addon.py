"""zine-maker — lay out a printable zine as PDF (PLAN.md §6.3).

Produces two PDFs into ``$DATA_DIR/output``:

* ``zine_sequential.pdf`` — pages in reading order, for the screen;
* ``zine_print.pdf`` — the same pages imposed onto sheets, for the printer.

The imposition and template maths live in :mod:`.layout` as pure functions; this module is the
plumbing around them: gather candidates, select, sequence, render, write.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import numpy as np
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import Field, model_validator

from immich_addons.addons.base import Addon, AddonConfig, AddonError
from immich_addons.core import scoring
from immich_addons.core.client import ImmichClient
from immich_addons.core.config import Settings, get_settings
from immich_addons.core.jobs import JobContext

from .layout import assign_templates, impose, photos_needed

log = logging.getLogger(__name__)

ADDON_ID = "zine-maker"
TAG = f"addon:{ADDON_ID}"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: Page geometry in millimetres: (width, height) of one *page*, before imposition.
PAGE_SIZES = {
    ("booklet", "portrait"): (148.0, 210.0),  # A5
    ("booklet", "landscape"): (210.0, 148.0),
    ("mini8", "portrait"): (74.0, 105.0),  # A7-ish, eight to an A4
    ("mini8", "landscape"): (105.0, 74.0),
}
BLEED_MM = 3.0


class ZineMakerConfig(AddonConfig):
    title: str = Field(default="", title="Title", description="Printed on the cover.")
    topic: str = Field(
        default="",
        title="Topic",
        description='Free-text search, e.g. "kids at the beach". Ignored when an album is chosen.',
    )
    album_id: str = Field(default="", title="Album", json_schema_extra={"x-picker": "albums"})
    pages: int = Field(default=8, title="Pages", json_schema_extra={"enum": [8, 16]})
    page_orientation: str = Field(
        default="portrait",
        title="Page orientation",
        json_schema_extra={"enum": ["portrait", "landscape"]},
    )
    layout: str = Field(
        default="mini8",
        title="Format",
        description=(
            "mini8 is one A4 sheet folded into an 8-page zine; "
            "booklet is A5 saddle-stitch from duplex A4."
        ),
        json_schema_extra={"enum": ["mini8", "booklet"]},
    )
    sequence: str = Field(
        default="chronological",
        title="Sequencing",
        description="arc groups similar scenes together instead of following the clock.",
        json_schema_extra={"enum": ["chronological", "arc"]},
    )
    captions: str = Field(
        default="exif",
        title="Captions",
        description="ollama needs OLLAMA_URL set; it falls back to exif when unreachable.",
        json_schema_extra={"enum": ["none", "exif", "ollama"]},
    )
    bleed_marks: bool = Field(default=True, title="Print bleed and cut marks")
    upload_pages: bool = Field(
        default=False,
        title="Upload page images to Immich",
        description='Creates an album "Zine: {title}" with one image per page.',
    )

    @model_validator(mode="after")
    def _check(self) -> ZineMakerConfig:
        if self.layout not in {"mini8", "booklet"}:
            raise ValueError("layout must be mini8 or booklet")
        if self.page_orientation not in {"portrait", "landscape"}:
            raise ValueError("page_orientation must be portrait or landscape")
        if self.pages not in {8, 16}:
            raise ValueError("pages must be 8 or 16")
        if not self.topic and not self.album_id:
            raise ValueError("give either a topic or an album")
        if self.layout == "mini8" and self.pages != 8:
            raise ValueError("the mini8 format is exactly 8 pages")
        return self


@dataclass
class Photo:
    """One selected photo, with everything the template needs."""

    asset_id: str
    filename: str
    aspect: float
    taken_at: datetime | None = None
    caption: str = ""
    data_uri: str = ""
    place: str = ""


@dataclass
class RenderedPage:
    index: int
    template: str
    photos: list[Photo] = field(default_factory=list)
    title: str = ""
    subtitle: str = ""


def sequence_photos(photos: list[Photo], mode: str, embeddings: np.ndarray | None) -> list[Photo]:
    """Order the photos for reading.

    ``chronological`` is the honest default — a zine that jumps around in time reads as a mistake.
    ``arc`` groups similar scenes and orders within each group by time, which suits a themed
    search better than a chronological one.
    """
    if mode == "chronological" or embeddings is None or len(photos) < 3:
        return sorted(photos, key=lambda p: (p.taken_at is None, p.taken_at or datetime.min))

    groups = scoring.near_dup_groups(embeddings, thr=0.75)
    ordered: list[Photo] = []
    for group in sorted(groups, key=lambda g: min(g)):
        members = [photos[i] for i in group]
        ordered.extend(
            sorted(members, key=lambda p: (p.taken_at is None, p.taken_at or datetime.min))
        )
    return ordered


def exif_caption(asset: dict[str, Any]) -> str:
    """Date and place, the two things worth printing under a photo."""
    exif = asset.get("exifInfo") or {}
    bits: list[str] = []
    raw = asset.get("fileCreatedAt") or exif.get("dateTimeOriginal")
    if raw:
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            stamp = None
        if stamp is not None:
            # No %-d: it is not portable to Windows, and the leading zero is not worth a branch.
            bits.append(stamp.strftime("%d %B %Y").lstrip("0"))
    place = ", ".join(str(exif[k]) for k in ("city", "state", "country") if exif.get(k))
    if place:
        bits.append(place)
    return " · ".join(bits)


def ollama_caption(
    url: str, image_bytes: bytes, *, model: str = "llava", timeout: float = 60.0
) -> str:
    """One-line caption from a local vision model. Returns "" on any failure — the caller falls
    back to EXIF, because a zine with plain dates beats a job that died on a caption."""
    if not url:
        return ""
    payload = {
        "model": model,
        "prompt": (
            "Describe this photo in one short caption, at most eight words. "
            "No punctuation at the end."
        ),
        "images": [base64.b64encode(image_bytes).decode()],
        "stream": False,
    }
    try:
        response = httpx.post(f"{url.rstrip('/')}/api/generate", json=payload, timeout=timeout)
        response.raise_for_status()
        text = str(response.json().get("response", "")).strip()
    except Exception as exc:  # noqa: BLE001 - captions are decorative, never fatal
        log.warning("ollama caption failed (%s); falling back to exif", exc)
        return ""
    return re.sub(r"\s+", " ", text).strip(" .\"'")[:80]


class ZineMaker(Addon):
    id: ClassVar[str] = ADDON_ID
    name: ClassVar[str] = "Zine Maker"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Lays out a printable zine from a search or an album: a folded single sheet "
        "or a saddle-stitch booklet, as print-ready PDF."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("manual",)
    config_model: ClassVar[type[AddonConfig]] = ZineMakerConfig

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        assert isinstance(config, ZineMakerConfig)

        with ImmichClient(self.settings, dry_run=config.dry_run) as client:
            ctx.progress(0.05, "gathering candidates")
            candidates = self._candidates(client, config)
            if not candidates:
                raise AddonError("no photos matched that topic or album")
            ctx.log(f"{len(candidates)} candidates")

            ctx.progress(0.2, "selecting")
            chosen = self._select(ctx, client, config, candidates)

            ctx.progress(0.45, "fetching images")
            photos = self._load(ctx, client, config, chosen)

            ctx.progress(0.7, "laying out")
            pages = self._paginate(config, photos)

            ctx.progress(0.8, "rendering")
            sequential, printable = self._render(ctx, config, pages)

            ctx.artifact("file", str(sequential), "sequential PDF (screen)")
            ctx.artifact("file", str(printable), "imposed PDF (print)")
            ctx.log(f"wrote {sequential.name} and {printable.name}")

            if config.upload_pages and not config.dry_run:
                ctx.log("page upload is not implemented yet; the PDFs are on disk")

    # --- steps ---------------------------------------------------------------------------

    def _candidates(self, client: ImmichClient, config: ZineMakerConfig) -> list[dict[str, Any]]:
        wanted = photos_needed(config.pages)
        if config.album_id:
            album = client.album_info(config.album_id)
            assets = [a for a in album.get("assets", []) if isinstance(a, dict)]
        else:
            assets = client.search_smart(config.topic, size=max(80, wanted * 3))
        return [
            a
            for a in assets
            if str(a.get("type", "IMAGE")).upper() == "IMAGE"
            and not any(
                str((t or {}).get("name", "")).startswith("addon:") for t in a.get("tags") or []
            )
        ]

    def _select(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: ZineMakerConfig,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        wanted = photos_needed(config.pages)
        if len(candidates) <= wanted:
            return candidates

        embeddings = self._embeddings(ctx, [str(a["id"]) for a in candidates])
        quality = np.array([_quality_hint(a) for a in candidates])
        kept = scoring.collapse_bursts(embeddings, quality)
        ctx.log(f"{len(candidates) - len(kept)} near-duplicate(s) collapsed")
        picks = scoring.mmr_select(embeddings[kept], quality[kept], wanted, lam=0.7)
        return [candidates[kept[i]] for i in picks]

    def _embeddings(self, ctx: JobContext, ids: list[str]) -> np.ndarray:
        from immich_addons.core import db

        try:
            vectors = db.embeddings_for(ids, settings=self.settings)
        except Exception as exc:  # noqa: BLE001 - a zine without CLIP is still a zine
            ctx.log(f"no embeddings available ({type(exc).__name__}); ordering by time only")
            vectors = {}
        width = len(next(iter(vectors.values()))) if vectors else 8
        rng = np.random.default_rng(0)
        return np.stack([vectors.get(i, rng.standard_normal(width) * 1e-3) for i in ids]).astype(
            np.float64
        )

    def _load(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: ZineMakerConfig,
        assets: list[dict[str, Any]],
    ) -> list[Photo]:
        from PIL import Image

        photos: list[Photo] = []
        for i, asset in enumerate(assets):
            ctx.progress(0.45 + 0.25 * i / max(1, len(assets)), "")
            asset_id = str(asset["id"])
            raw = client.thumbnail(asset_id, size="preview")
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
            caption = ""
            if config.captions == "ollama":
                caption = ollama_caption(self.settings.ollama_url, raw)
            if not caption and config.captions in {"exif", "ollama"}:
                caption = exif_caption(asset)

            photos.append(
                Photo(
                    asset_id=asset_id,
                    filename=str(asset.get("originalFileName") or asset_id),
                    aspect=width / height if height else 1.0,
                    taken_at=_taken_at(asset),
                    caption=caption,
                    data_uri="data:image/jpeg;base64," + base64.b64encode(raw).decode(),
                )
            )
        return photos

    def _paginate(self, config: ZineMakerConfig, photos: list[Photo]) -> list[RenderedPage]:
        page_w, page_h = PAGE_SIZES[(config.layout, config.page_orientation)]
        ordered = sequence_photos(photos, config.sequence, None)
        assignment = assign_templates(
            [p.aspect for p in ordered], config.pages, page_aspect=page_w / page_h
        )

        pages: list[RenderedPage] = []
        for index, (template, indices) in enumerate(assignment, start=1):
            page = RenderedPage(
                index=index,
                template=template.name,
                photos=[ordered[i] for i in indices],
            )
            if template.name == "cover":
                page.title = config.title or (config.topic or "Zine")
                page.subtitle = _date_span(ordered)
            elif template.name == "colophon":
                page.title = config.title or ""
                page.subtitle = f"{len(ordered)} photographs · made with immich-addons"
            pages.append(page)
        return pages

    def _render(
        self, ctx: JobContext, config: ZineMakerConfig, pages: list[RenderedPage]
    ) -> tuple[Path, Path]:
        env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            autoescape=select_autoescape(["html"]),
        )
        page_w, page_h = PAGE_SIZES[(config.layout, config.page_orientation)]
        sheets = impose(config.pages, config.layout)

        common = {
            "config": config,
            "pages": pages,
            "page_w": page_w,
            "page_h": page_h,
            "bleed": BLEED_MM if config.bleed_marks else 0.0,
        }
        sequential_html = env.get_template("sequential.html").render(**common)
        print_html = env.get_template("imposed.html").render(
            **common,
            sheets=[s.to_json() for s in sheets],
            page_by_number={p.index: p for p in pages},
        )

        out = self.settings.output_dir
        out.mkdir(parents=True, exist_ok=True)
        stem = _slug(config.title or config.topic or "zine")
        sequential = out / f"{stem}_sequential.pdf"
        printable = out / f"{stem}_print.pdf"

        _write_pdf(sequential_html, sequential)
        _write_pdf(print_html, printable)
        return sequential, printable


def _write_pdf(html: str, dest: Path) -> None:
    """Render HTML to PDF. WeasyPrint is imported here so the addon page still loads without it."""
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on system libraries
        raise AddonError(
            "WeasyPrint is not available. It ships in the hub's Docker image; on a bare "
            "workstation it also needs the Pango/Cairo system libraries."
        ) from exc
    HTML(string=html).write_pdf(dest)


def _quality_hint(asset: dict[str, Any]) -> float:
    """A cheap stand-in for full quality scoring: favour bigger, favourited images."""
    exif = asset.get("exifInfo") or {}
    pixels = float(exif.get("exifImageWidth") or 0) * float(exif.get("exifImageHeight") or 0)
    return (1.0 if asset.get("isFavorite") else 0.0) + min(1.0, pixels / 24e6)


def _taken_at(asset: dict[str, Any]) -> datetime | None:
    raw = asset.get("fileCreatedAt") or (asset.get("exifInfo") or {}).get("dateTimeOriginal")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _date_span(photos: list[Photo]) -> str:
    stamps = sorted(p.taken_at for p in photos if p.taken_at)
    if not stamps:
        return ""
    if stamps[0].date() == stamps[-1].date():
        return stamps[0].strftime("%d %B %Y")
    return f"{stamps[0]:%d %B %Y} – {stamps[-1]:%d %B %Y}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "zine"


__all__ = ["ZineMaker", "ZineMakerConfig", "exif_caption", "ollama_caption", "sequence_photos"]
