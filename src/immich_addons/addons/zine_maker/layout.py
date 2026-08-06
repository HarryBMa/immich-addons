"""Imposition and page-template assignment for zine-maker (PLAN.md §6.3).

Two independent problems, both pure functions so they can be checked against golden files and,
for the folded formats, against a hand-drawn diagram:

* :func:`impose` — where each page number lands on the printed sheet. Imposition bugs are
  invisible on screen and only show up after folding, so this is golden-tested;
* :func:`assign_templates` — which layout each page gets, chosen to waste as little of each photo
  as possible. This is what makes portrait *and* landscape photos both work in one zine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# --- imposition -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Panel:
    """One page placed on a printed side."""

    page: int
    rotated: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Sheet:
    """One printed side of one physical sheet."""

    sheet: int
    side: str  # "front" | "back"
    panels: tuple[Panel, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "side": self.side,
            "panels": [p.to_json() for p in self.panels],
        }


def impose_booklet(n_pages: int) -> list[Sheet]:
    """Saddle-stitch imposition for any ``n_pages`` divisible by four.

    Fold a stack of sheets in half and staple the spine: the outermost sheet carries the first and
    last pages, the next carries the second and second-to-last, and so on inwards. So each printed
    side holds the pair ``(i, n + 1 - i)``, and the sides run
    ``(n, 1), (2, n - 1), (n - 2, 3), (4, n - 3), …``.

    That is why **page 1 and page 16 share a printed side** in a 16-page booklet: they are the
    outside of the outermost sheet — the back cover and the front cover, which become adjacent once
    the sheet is folded.

    Duplex printing flips on the **short edge**, so front and back of a sheet stay upright.
    """
    if n_pages <= 0 or n_pages % 4 != 0:
        raise ValueError(f"a saddle-stitch booklet needs a multiple of 4 pages, got {n_pages}")

    sheets: list[Sheet] = []
    left, right = n_pages, 1  # the outermost pair
    for index in range(n_pages // 4):
        # Front of this sheet: the high page on the left, the low page on the right.
        sheets.append(Sheet(index + 1, "front", (Panel(left), Panel(right))))
        # Back: the two pages that face each other when the sheet is turned over.
        sheets.append(Sheet(index + 1, "back", (Panel(right + 1), Panel(left - 1))))
        left -= 2
        right += 2
    return sheets


#: The classic single-sheet zine: 2 rows x 4 columns on one landscape A4, printed single-sided,
#: folded three times with one cut across the middle. The top row is upside down, which is what
#: makes the folding work.
MINI8_TOP = (5, 4, 3, 2)
MINI8_BOTTOM = (6, 7, 8, 1)


def impose_mini8() -> list[Sheet]:
    """The eight-panel folded zine: one sheet, one side, top row rotated 180°."""
    panels = tuple(Panel(page, rotated=True) for page in MINI8_TOP) + tuple(
        Panel(page) for page in MINI8_BOTTOM
    )
    return [Sheet(1, "front", panels)]


def impose(n_pages: int, layout: str = "booklet") -> list[Sheet]:
    """Dispatch to the right imposition. ``layout`` is ``"mini8"`` or ``"booklet"``."""
    if layout == "mini8":
        if n_pages != 8:
            raise ValueError(f"the mini8 format is exactly 8 pages, got {n_pages}")
        return impose_mini8()
    if layout == "booklet":
        return impose_booklet(n_pages)
    raise ValueError(f"unknown layout {layout!r}")


def cut_line(layout: str) -> str:
    """Where the printed cut mark goes, as a note for the rendering template."""
    return "centre-horizontal-middle-columns" if layout == "mini8" else ""


# --- page templates -------------------------------------------------------------------------


@dataclass(frozen=True)
class PageTemplate:
    """A page layout and how many photos it holds."""

    name: str
    slots: int
    #: Aspect ratio (width / height) each slot suits best. 0 means "anything".
    prefers: float


FULL_BLEED = PageTemplate("full-bleed", 1, 0.0)
TWO_UP_VERTICAL = PageTemplate("two-up-vertical", 2, 1.5)  # two landscape photos stacked
TWO_UP_HORIZONTAL = PageTemplate("two-up-horizontal", 2, 0.67)  # two portraits side by side
FOUR_GRID = PageTemplate("four-grid", 4, 1.0)
COVER = PageTemplate("cover", 1, 0.0)
COLOPHON = PageTemplate("colophon", 0, 0.0)

CONTENT_TEMPLATES = (FULL_BLEED, TWO_UP_VERTICAL, TWO_UP_HORIZONTAL, FOUR_GRID)


def crop_loss(photo_aspect: float, slot_aspect: float) -> float:
    """Fraction of the photo lost when filling a slot of a different shape.

    Filling by cropping: the photo is scaled to cover the slot and the overflow is cut. A 3:2
    landscape in a 2:3 portrait slot loses more than half its width, which is why template choice
    matters more than it looks.
    """
    if photo_aspect <= 0 or slot_aspect <= 0:
        return 1.0
    ratio = photo_aspect / slot_aspect
    return 1.0 - (1.0 / ratio if ratio > 1 else ratio)


def assign_templates(
    aspects: list[float],
    page_count: int,
    *,
    page_aspect: float = 0.707,  # A5 portrait: 148/210
    with_cover: bool = True,
) -> list[tuple[PageTemplate, list[int]]]:
    """Lay ``aspects`` out over ``page_count`` pages, minimising crop loss.

    Greedy: for each page, try every template, score the best assignment of the next photos to its
    slots, and take the cheapest. Greedy rather than optimal because the sequence is already fixed
    by the time we get here — photos must stay in order, so there is nothing to gain from
    reordering, and a page's choice barely affects later pages.

    Returns one ``(template, photo indices)`` per page. The first page is the cover and the last is
    the colophon when ``with_cover`` is set.
    """
    if page_count <= 0:
        return []

    pages: list[tuple[PageTemplate, list[int]]] = []
    cursor = 0
    content_pages = page_count - (2 if with_cover else 0)

    if with_cover:
        pages.append((COVER, [0] if aspects else []))
        cursor = 1 if aspects else 0

    remaining_pages = max(0, content_pages)
    for page_index in range(remaining_pages):
        left = len(aspects) - cursor
        if left <= 0:
            pages.append((FULL_BLEED, []))
            continue

        pages_left = remaining_pages - page_index
        best: tuple[float, PageTemplate] | None = None
        for template in CONTENT_TEMPLATES:
            if template.slots > left:
                continue
            # Do not spend all the photos early and leave later pages empty.
            if left - template.slots < pages_left - 1 and pages_left > 1:
                continue
            slot_aspect = template.prefers or page_aspect
            loss = (
                sum(crop_loss(aspects[cursor + i], slot_aspect) for i in range(template.slots))
                / template.slots
            )
            if best is None or loss < best[0] - 1e-9:
                best = (loss, template)

        template = best[1] if best else FULL_BLEED
        take = min(template.slots, left)
        pages.append((template, list(range(cursor, cursor + take))))
        cursor += take

    if with_cover:
        pages.append((COLOPHON, []))
    return pages


def photos_needed(page_count: int, *, with_cover: bool = True) -> int:
    """Roughly how many photos to gather for a zine of this length.

    Errs high: PLAN.md §6.3 asks for ~1.3x the needed count so the selection has something to
    discard.
    """
    content = max(0, page_count - (2 if with_cover else 0))
    return int((content * 2 + (1 if with_cover else 0)) * 1.3) + 1
