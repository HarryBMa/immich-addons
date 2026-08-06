"""Imposition, against golden files and against how the paper actually folds.

Imposition bugs are invisible on screen — the sequential PDF looks perfect while the printed sheet
folds into nonsense. So these tests check the matrices against committed goldens *and* against the
physical properties folding imposes, which is the part a golden file cannot tell you is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from immich_addons.addons.zine_maker.layout import (
    MINI8_BOTTOM,
    MINI8_TOP,
    assign_templates,
    crop_loss,
    impose,
    impose_booklet,
    impose_mini8,
    photos_needed,
)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"


def _as_json(sheets) -> list[dict]:  # noqa: ANN001
    return [sheet.to_json() for sheet in sheets]


# --- booklet --------------------------------------------------------------------------------


@pytest.mark.parametrize("n_pages", [8, 16])
def test_booklet_matches_its_golden_file(n_pages: int) -> None:
    expected = json.loads((GOLDEN / f"booklet-{n_pages}.json").read_text(encoding="utf-8"))
    assert _as_json(impose_booklet(n_pages)) == expected


def test_page_one_and_the_last_page_share_a_printed_side() -> None:
    """They are the outside of the outermost sheet: back cover and front cover, adjacent once
    the sheet is folded. This is the property most easily got wrong."""
    front = impose_booklet(16)[0]
    assert {p.page for p in front.panels} == {16, 1}


def test_every_page_appears_exactly_once() -> None:
    for n_pages in (4, 8, 12, 16, 40):
        pages = [p.page for sheet in impose_booklet(n_pages) for p in sheet.panels]
        assert sorted(pages) == list(range(1, n_pages + 1))


def test_each_printed_side_holds_a_complementary_pair() -> None:
    """PLAN.md §6.3: every side holds ``(i, n + 1 - i)``."""
    for n_pages in (8, 16, 24):
        for sheet in impose_booklet(n_pages):
            a, b = (p.page for p in sheet.panels)
            assert a + b == n_pages + 1


def test_sheet_count_is_pages_over_four() -> None:
    for n_pages in (4, 8, 16, 40):
        sheets = impose_booklet(n_pages)
        assert len({s.sheet for s in sheets}) == n_pages // 4
        assert len(sheets) == n_pages // 2  # two printed sides per sheet


def test_every_sheet_has_a_front_and_a_back() -> None:
    sides: dict[int, set[str]] = {}
    for sheet in impose_booklet(16):
        sides.setdefault(sheet.sheet, set()).add(sheet.side)
    assert all(s == {"front", "back"} for s in sides.values())


def test_consecutive_pages_face_each_other_when_folded() -> None:
    """Pages 2 and 3 must end up as a spread: 2 on the back of sheet 1, 3 on the front of sheet 2.

    Checked as: for every inner pair, the two pages sit on different printed sides but adjacent
    positions in the fold order.
    """
    sheets = impose_booklet(8)
    placement = {p.page: (sheet.sheet, sheet.side) for sheet in sheets for p in sheet.panels}
    assert placement[1] == (1, "front")
    assert placement[8] == (1, "front")
    assert placement[2] == (1, "back")
    assert placement[7] == (1, "back")
    assert placement[3] == (2, "front")
    assert placement[6] == (2, "front")
    assert placement[4] == (2, "back")
    assert placement[5] == (2, "back")


@pytest.mark.parametrize("bad", [0, 3, 6, 10, -4])
def test_a_page_count_that_cannot_be_folded_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="multiple of 4"):
        impose_booklet(bad)


# --- mini8 ----------------------------------------------------------------------------------


def test_mini8_matches_its_golden_file() -> None:
    expected = json.loads((GOLDEN / "mini8.json").read_text(encoding="utf-8"))
    assert _as_json(impose_mini8()) == expected


def test_mini8_is_one_single_sided_sheet() -> None:
    sheets = impose_mini8()
    assert len(sheets) == 1
    assert sheets[0].side == "front"
    assert len(sheets[0].panels) == 8


def test_mini8_uses_every_page_once() -> None:
    pages = [p.page for p in impose_mini8()[0].panels]
    assert sorted(pages) == list(range(1, 9))


def test_the_top_row_is_upside_down() -> None:
    """The rotation is what makes the fold work; without it half the zine reads inverted."""
    panels = impose_mini8()[0].panels
    top, bottom = panels[:4], panels[4:]
    assert all(p.rotated for p in top)
    assert not any(p.rotated for p in bottom)
    assert tuple(p.page for p in top) == MINI8_TOP
    assert tuple(p.page for p in bottom) == MINI8_BOTTOM


def test_page_one_sits_at_the_bottom_right() -> None:
    """The cover ends up on the outside of the fold, which puts it in the last bottom cell."""
    panels = impose_mini8()[0].panels
    assert panels[-1].page == 1


def test_mini8_rejects_any_other_page_count() -> None:
    with pytest.raises(ValueError, match="exactly 8 pages"):
        impose(16, layout="mini8")


def test_unknown_layout_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown layout"):
        impose(8, layout="origami")


# --- template assignment --------------------------------------------------------------------


def test_crop_loss_is_zero_for_a_matching_aspect() -> None:
    assert crop_loss(1.5, 1.5) == pytest.approx(0.0)


def test_crop_loss_grows_with_mismatch() -> None:
    assert crop_loss(1.5, 0.67) > crop_loss(1.5, 1.0) > crop_loss(1.5, 1.4)


def test_landscape_photos_prefer_the_stacked_template() -> None:
    """Two landscapes stack; two portraits sit side by side. Getting this backwards is the bug
    that makes a zine full of half-cropped photos."""
    pages = assign_templates([1.5] * 6, page_count=5, with_cover=False)
    assert any(template.name == "two-up-vertical" for template, _ in pages)


def test_portrait_photos_prefer_the_side_by_side_template() -> None:
    pages = assign_templates([0.67] * 6, page_count=5, with_cover=False)
    assert any(template.name == "two-up-horizontal" for template, _ in pages)


def test_the_cover_and_colophon_bracket_the_zine() -> None:
    pages = assign_templates([1.5] * 10, page_count=8)
    assert pages[0][0].name == "cover"
    assert pages[-1][0].name == "colophon"
    assert len(pages) == 8


def test_no_photo_is_used_twice() -> None:
    pages = assign_templates([1.5, 0.67, 1.0, 1.5, 0.67, 1.0, 1.3, 0.8], page_count=8)
    used = [i for _, indices in pages for i in indices]
    assert len(used) == len(set(used))


def test_photos_are_not_all_spent_on_the_early_pages() -> None:
    """A four-grid on page 2 would leave later pages empty; the assignment must look ahead."""
    pages = assign_templates([1.0] * 5, page_count=7, with_cover=False)
    filled = [indices for _, indices in pages if indices]
    assert len(filled) >= 4, "photos were bunched onto too few pages"


def test_running_out_of_photos_leaves_empty_pages_rather_than_failing() -> None:
    pages = assign_templates([1.5], page_count=6, with_cover=False)
    assert len(pages) == 6


def test_no_pages_at_all() -> None:
    assert assign_templates([1.5], page_count=0) == []


def test_photos_needed_errs_high() -> None:
    """§6.3 gathers ~1.3x what it needs, so the selection has something to discard."""
    assert photos_needed(8) > 8
    assert photos_needed(16) > photos_needed(8)
