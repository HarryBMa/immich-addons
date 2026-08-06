"""zine-maker: captions, sequencing, and HTML rendering (PDF where WeasyPrint is available)."""

from __future__ import annotations

import base64
import io
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from immich_addons.addons.zine_maker.addon import (
    PAGE_SIZES,
    Photo,
    ZineMaker,
    ZineMakerConfig,
    exif_caption,
    ollama_caption,
    sequence_photos,
)
from immich_addons.core.client import ImmichClient
from immich_addons.core.jobs import JobQueue, JobStatus


def _thumbnail(width: int = 120, height: int = 80) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 120, 150)).save(buffer, format="JPEG")
    return buffer.getvalue()


# --- captions -------------------------------------------------------------------------------


def test_exif_caption_uses_date_and_place() -> None:
    caption = exif_caption(
        {
            "fileCreatedAt": "2026-07-14T10:30:00+00:00",
            "exifInfo": {"city": "Visby", "country": "Sweden"},
        }
    )
    assert "July 2026" in caption
    assert "Visby, Sweden" in caption


def test_exif_caption_copes_with_nothing_useful() -> None:
    assert exif_caption({}) == ""
    assert exif_caption({"fileCreatedAt": "not a date"}) == ""


def test_ollama_caption_is_returned_when_it_works(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        payload = kwargs["json"]
        assert base64.b64decode(payload["images"][0])  # the image is actually sent
        # raise_for_status() needs a request on the response, so attach one.
        return httpx.Response(
            200,
            json={"response": "  Kids running into  the sea.  "},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("immich_addons.addons.zine_maker.addon.httpx.post", fake_post)
    assert ollama_caption("http://ollama.test:11434", b"jpeg") == "Kids running into the sea"


def test_ollama_failure_falls_back_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zine with plain dates beats a job that died on a caption."""

    def boom(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("immich_addons.addons.zine_maker.addon.httpx.post", boom)
    assert ollama_caption("http://ollama.test:11434", b"jpeg") == ""


def test_no_ollama_url_means_no_request() -> None:
    assert ollama_caption("", b"jpeg") == ""


# --- sequencing -----------------------------------------------------------------------------


def _photo(index: int, day: int, aspect: float = 1.5) -> Photo:
    return Photo(
        asset_id=f"a{index}",
        filename=f"IMG_{index}.jpg",
        aspect=aspect,
        taken_at=datetime(2026, 7, day, 12, 0, tzinfo=UTC),
    )


def test_chronological_is_the_default_order() -> None:
    photos = [_photo(0, 14), _photo(1, 2), _photo(2, 9)]
    assert [p.asset_id for p in sequence_photos(photos, "chronological", None)] == [
        "a1",
        "a2",
        "a0",
    ]


def test_photos_without_a_date_sort_last() -> None:
    undated = Photo(asset_id="x", filename="x.jpg", aspect=1.5)
    ordered = sequence_photos([undated, _photo(0, 3)], "chronological", None)
    assert ordered[-1].asset_id == "x"


def test_arc_groups_similar_scenes_together() -> None:
    """Two scenes shot alternately: arc mode should un-interleave them."""
    photos = [_photo(i, i + 1) for i in range(4)]
    a, b = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    embeddings = np.stack([a, b, a + 0.01, b + 0.01])
    ordered = [p.asset_id for p in sequence_photos(photos, "arc", embeddings)]
    assert ordered.index("a2") - ordered.index("a0") == 1
    assert abs(ordered.index("a3") - ordered.index("a1")) == 1


def test_arc_falls_back_when_there_is_nothing_to_cluster() -> None:
    photos = [_photo(0, 5), _photo(1, 1)]
    assert [p.asset_id for p in sequence_photos(photos, "arc", None)] == ["a1", "a0"]


# --- config ---------------------------------------------------------------------------------


def test_mini8_must_be_eight_pages() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="mini8"):
        ZineMakerConfig(topic="beach", layout="mini8", pages=16)


def test_a_source_is_required() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="topic or an album"):
        ZineMakerConfig()


def test_every_format_and_orientation_has_a_page_size() -> None:
    for layout in ("mini8", "booklet"):
        for orientation in ("portrait", "landscape"):
            assert PAGE_SIZES[(layout, orientation)][0] > 0


# --- rendering ------------------------------------------------------------------------------


@pytest.fixture
def zine_stack(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    assets = [
        {
            "id": f"a{i}",
            "type": "IMAGE",
            "originalFileName": f"IMG_{i}.jpg",
            "tags": [],
            "fileCreatedAt": f"2026-07-{i + 1:02d}T12:00:00+00:00",
            "exifInfo": {
                "city": "Visby",
                "country": "Sweden",
                "exifImageWidth": 6000,
                "exifImageHeight": 4000,
            },
        }
        for i in range(14)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/search/smart":
            return httpx.Response(200, json={"assets": {"items": assets}})
        if path == "/api/albums/album-1":
            return httpx.Response(200, json={"albumName": "Gotland", "assets": assets})
        if "/thumbnail" in path:
            index = int(path.split("/")[3][1:])
            # Alternate portrait and landscape so template assignment has to make a choice.
            return httpx.Response(
                200, content=_thumbnail(120, 80) if index % 2 == 0 else _thumbnail(80, 120)
            )
        return httpx.Response(200, json={})

    from immich_addons.addons.zine_maker import addon as module
    from immich_addons.core import db

    monkeypatch.setattr(db, "embeddings_for", lambda ids, **kw: {})
    return httpx.MockTransport(handler), module


def _paginate(settings, config, transport, module, monkeypatch):  # noqa: ANN001, ANN202
    """Run the pipeline as far as pagination, which needs no PDF engine."""
    monkeypatch.setattr(
        module,
        "ImmichClient",
        lambda s, dry_run=False: ImmichClient(s, dry_run=dry_run, transport=transport),
    )
    addon = ZineMaker(settings)
    with ImmichClient(settings, dry_run=True, transport=transport) as client:
        candidates = addon._candidates(client, config)

        class _Ctx:
            params: dict = {}

            def progress(self, *a: object, **k: object) -> None: ...
            def log(self, *a: object, **k: object) -> None: ...

        photos = addon._load(_Ctx(), client, config, candidates[: config.pages])
    return addon._paginate(config, photos)


@pytest.mark.parametrize("layout,pages", [("mini8", 8), ("booklet", 8), ("booklet", 16)])
@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_every_format_and_orientation_paginates(
    settings,
    zine_stack,
    monkeypatch,
    layout: str,
    pages: int,
    orientation: str,  # noqa: ANN001
) -> None:
    """§6.3 acceptance: both formats x both orientations produce a full page set."""
    transport, module = zine_stack
    config = ZineMakerConfig(
        topic="beach", layout=layout, pages=pages, page_orientation=orientation, dry_run=True
    )
    rendered = _paginate(settings, config, transport, module, monkeypatch)

    assert len(rendered) == pages
    assert rendered[0].template == "cover"
    assert rendered[-1].template == "colophon"
    assert rendered[0].title


def test_captions_are_attached_from_exif(settings, zine_stack, monkeypatch) -> None:  # noqa: ANN001
    transport, module = zine_stack
    config = ZineMakerConfig(topic="beach", captions="exif", dry_run=True)
    rendered = _paginate(settings, config, transport, module, monkeypatch)
    captions = [p.caption for page in rendered for p in page.photos]
    assert any("Visby" in c for c in captions)


def test_captions_can_be_turned_off(settings, zine_stack, monkeypatch) -> None:  # noqa: ANN001
    transport, module = zine_stack
    config = ZineMakerConfig(topic="beach", captions="none", dry_run=True)
    rendered = _paginate(settings, config, transport, module, monkeypatch)
    assert all(p.caption == "" for page in rendered for p in page.photos)


def test_the_html_renders_with_every_page_present(settings, zine_stack, monkeypatch) -> None:  # noqa: ANN001
    """Render the templates without a PDF engine, so the HTML itself is always checked."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from immich_addons.addons.zine_maker.addon import TEMPLATES_DIR
    from immich_addons.addons.zine_maker.layout import impose

    transport, module = zine_stack
    config = ZineMakerConfig(title="Gotland", topic="beach", layout="mini8", dry_run=True)
    pages = _paginate(settings, config, transport, module, monkeypatch)

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html"])
    )
    page_w, page_h = PAGE_SIZES[("mini8", "portrait")]
    common = {"config": config, "pages": pages, "page_w": page_w, "page_h": page_h, "bleed": 3.0}

    sequential = env.get_template("sequential.html").render(**common)
    assert sequential.count('class="page"') == 8
    assert "Gotland" in sequential

    imposed = env.get_template("imposed.html").render(
        **common,
        sheets=[s.to_json() for s in impose(8, "mini8")],
        page_by_number={p.index: p for p in pages},
    )
    assert imposed.count('class="cell') == 8
    assert imposed.count("rotated") >= 4, "the top row must be rotated"
    assert "cutline" in imposed, "the single centre cut must be marked"


def _weasyprint_works() -> bool:
    """Importing WeasyPrint is not enough: on Windows it imports fine and then fails to load
    Pango/Cairo at render time. So actually render something."""
    try:
        from weasyprint import HTML

        HTML(string="<p>x</p>").write_pdf()
    except Exception:  # noqa: BLE001 - any failure means "not usable here"
        return False
    return True


HAVE_WEASYPRINT = _weasyprint_works()


@pytest.mark.skipif(not HAVE_WEASYPRINT, reason="WeasyPrint cannot render here")
def test_pdf_output(settings, zine_stack, monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    """The real thing, when WeasyPrint can render. Skipped otherwise."""
    transport, module = zine_stack
    settings.ensure_data_dirs()
    config = ZineMakerConfig(title="Gotland", topic="beach", layout="mini8", dry_run=True)

    monkeypatch.setattr(
        module,
        "ImmichClient",
        lambda s, dry_run=False: ImmichClient(s, dry_run=dry_run, transport=transport),
    )
    jobs = JobQueue(tmp_path / "j.sqlite")
    addon = ZineMaker(settings)
    jobs.register("zine-maker", lambda ctx: addon.run(ctx, config))
    job_id = jobs.enqueue("zine-maker", {})
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None and job.status is JobStatus.DONE, job.log
    files = [Path(a["value"]) for a in job.artifacts if a["kind"] == "file"]
    assert len(files) == 2
    for path in files:
        assert path.exists()
        assert path.read_bytes().startswith(b"%PDF")


def test_a_missing_pdf_engine_fails_with_a_readable_message(
    settings,
    zine_stack,
    monkeypatch,
    tmp_path: Path,  # noqa: ANN001
) -> None:
    transport, module = zine_stack

    def no_weasyprint(html: str, dest: Path) -> None:
        from immich_addons.addons.base import AddonError

        raise AddonError("WeasyPrint is not available.")

    monkeypatch.setattr(module, "_write_pdf", no_weasyprint)
    monkeypatch.setattr(
        module,
        "ImmichClient",
        lambda s, dry_run=False: ImmichClient(s, dry_run=dry_run, transport=transport),
    )
    settings.ensure_data_dirs()
    jobs = JobQueue(tmp_path / "j.sqlite")
    addon = ZineMaker(settings)
    config = ZineMakerConfig(topic="beach", dry_run=True)
    jobs.register("zine-maker", lambda ctx: addon.run(ctx, config))
    job_id = jobs.enqueue("zine-maker", {})
    jobs.run_job(job_id)

    job = jobs.get(job_id)
    assert job is not None and job.status is JobStatus.FAILED
    assert "WeasyPrint" in job.log
