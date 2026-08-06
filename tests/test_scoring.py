"""Selection maths, against synthetic data where the right answer is known."""

from __future__ import annotations

import numpy as np
import pytest

from immich_addons.core.scoring import (
    collapse_bursts,
    exposure,
    mmr_select,
    near_dup_groups,
    normalise_rows,
    quality_scores,
    sharpness,
    similarity_matrix,
)

RNG = np.random.default_rng(20260806)


def _checkerboard(size: int = 256, square: int = 8) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size]
    return (((x // square) + (y // square)) % 2 * 255).astype(np.float64)


def _blur(image: np.ndarray, passes: int = 3) -> np.ndarray:
    out = image.copy()
    for _ in range(passes):
        out = (
            out
            + np.roll(out, 1, axis=0)
            + np.roll(out, -1, axis=0)
            + np.roll(out, 1, axis=1)
            + np.roll(out, -1, axis=1)
        ) / 5.0
    return out


# --- sharpness ------------------------------------------------------------------------------


def test_a_blurred_image_scores_lower_than_a_sharp_one() -> None:
    sharp = _checkerboard()
    assert sharpness(sharp) > sharpness(_blur(sharp))


def test_a_flat_image_has_almost_no_sharpness() -> None:
    assert sharpness(np.full((128, 128), 128.0)) == pytest.approx(0.0, abs=1e-9)


def test_sharpness_is_resolution_independent() -> None:
    """A 24 MP frame and a 12 MP frame of the same scene should score alike."""
    small = _checkerboard(size=720, square=24)
    large = np.repeat(np.repeat(small, 2, axis=0), 2, axis=1)
    assert sharpness(large) == pytest.approx(sharpness(small), rel=0.35)


def test_sharpness_accepts_rgb_and_0_1_ranges() -> None:
    grey = _checkerboard(size=64)
    rgb = np.stack([grey] * 3, axis=-1)
    assert sharpness(rgb) > 0
    assert sharpness(grey / 255.0) == pytest.approx(sharpness(grey), rel=1e-6)


def test_sharpness_rejects_nonsense_shapes() -> None:
    with pytest.raises(ValueError, match="2D or 3D"):
        sharpness(np.zeros((2, 2, 2, 2)))


# --- exposure -------------------------------------------------------------------------------


def test_a_well_exposed_image_scores_one() -> None:
    assert exposure(np.full((64, 64), 128.0)) == pytest.approx(1.0)


def test_blown_highlights_are_penalised() -> None:
    image = np.full((100, 100), 128.0)
    image[:25] = 255.0
    assert exposure(image) == pytest.approx(0.75)


def test_crushed_shadows_are_penalised_too() -> None:
    """Both ends are lost information, so both count."""
    image = np.full((100, 100), 128.0)
    image[:25] = 0.0
    assert exposure(image) == pytest.approx(0.75)


# --- near-duplicate grouping ----------------------------------------------------------------


def _burst(base: np.ndarray, count: int, jitter: float) -> np.ndarray:
    return np.stack([base + jitter * RNG.standard_normal(base.shape) for _ in range(count)])


def test_a_burst_collapses_to_one_group() -> None:
    """PLAN.md §6.2 acceptance: five near-identical frames become one pick."""
    base = RNG.standard_normal(64)
    embeddings = _burst(base, 5, jitter=0.02)
    assert near_dup_groups(embeddings) == [[0, 1, 2, 3, 4]]


def test_distinct_scenes_stay_apart() -> None:
    embeddings = RNG.standard_normal((6, 64))
    assert near_dup_groups(embeddings) == [[0], [1], [2], [3], [4], [5]]


def test_bursts_and_singles_together() -> None:
    scene_a, scene_b = RNG.standard_normal(64), RNG.standard_normal(64)
    embeddings = np.vstack([_burst(scene_a, 3, 0.02), scene_b[None, :], _burst(scene_a, 2, 0.02)])
    groups = near_dup_groups(embeddings)
    assert [3] in groups
    assert sorted(sum((g for g in groups if g != [3]), [])) == [0, 1, 2, 4, 5]


def test_grouping_is_transitive() -> None:
    """A burst that drifts frame to frame must still collapse: A~B and B~C means one group."""
    base = RNG.standard_normal(64)
    base /= np.linalg.norm(base)
    drift = RNG.standard_normal(64)
    drift /= np.linalg.norm(drift)
    embeddings = np.stack([base + i * 0.12 * drift for i in range(4)])
    assert near_dup_groups(embeddings, thr=0.97) == [[0, 1, 2, 3]]


def test_every_index_appears_exactly_once() -> None:
    embeddings = RNG.standard_normal((12, 32))
    flat = sorted(sum(near_dup_groups(embeddings, thr=0.5), []))
    assert flat == list(range(12))


def test_threshold_controls_aggressiveness() -> None:
    embeddings = RNG.standard_normal((8, 32))
    assert len(near_dup_groups(embeddings, thr=0.99)) >= len(near_dup_groups(embeddings, thr=0.1))


def test_empty_input() -> None:
    assert near_dup_groups(np.zeros((0, 8))) == []


def test_collapse_keeps_the_best_member_of_each_burst() -> None:
    base = RNG.standard_normal(64)
    embeddings = _burst(base, 5, jitter=0.02)
    quality = [0.1, 0.2, 0.9, 0.3, 0.2]  # frame 2 is the keeper
    assert collapse_bursts(embeddings, quality) == [2]


# --- MMR ------------------------------------------------------------------------------------


def test_lambda_one_is_a_pure_quality_ranking() -> None:
    embeddings = RNG.standard_normal((6, 32))
    quality = [0.1, 0.9, 0.5, 0.2, 0.8, 0.3]
    assert mmr_select(embeddings, quality, n=3, lam=1.0) == [1, 4, 2]


def test_lambda_zero_spreads_as_widely_as_possible() -> None:
    """Two tight clusters: pure diversity must take one from each before a second from either."""
    a, b = RNG.standard_normal(32), RNG.standard_normal(32)
    embeddings = np.stack([a, a + 0.01, a + 0.02, b, b + 0.01, b + 0.02])
    picks = mmr_select(embeddings, [1.0, 0.9, 0.8, 0.7, 0.6, 0.5], n=2, lam=0.0)
    assert picks[0] in {0, 1, 2}
    assert picks[1] in {3, 4, 5}


def test_at_the_default_lambda_a_near_duplicate_can_still_win() -> None:
    """This is why `collapse_bursts` runs *before* `mmr_select`, not instead of it.

    With lam=0.7 the diversity term is worth at most 0.3, so a burst frame holding 96% of the
    best frame's quality beats a distinct scene at 50%. MMR buys variety between *scenes*; it is
    not a burst filter, and relying on it as one would fill an album with one afternoon.
    """
    a, b = RNG.standard_normal(32), RNG.standard_normal(32)
    embeddings = np.stack([a, a + 0.01, a + 0.02, b])
    quality = [1.0, 0.98, 0.96, 0.5]

    assert mmr_select(embeddings, quality, n=2, lam=0.7)[1] in {1, 2}

    # Collapse the burst first — as the addons do — and the distinct scene is all that is left.
    kept = collapse_bursts(embeddings, quality)
    assert kept == [0, 3]


def test_lowering_lambda_buys_variety() -> None:
    a, b = RNG.standard_normal(32), RNG.standard_normal(32)
    embeddings = np.stack([a, a + 0.01, a + 0.02, b])
    quality = [1.0, 0.98, 0.96, 0.5]
    assert 3 in mmr_select(embeddings, quality, n=2, lam=0.3)


def test_the_first_pick_is_always_the_best_item() -> None:
    embeddings = RNG.standard_normal((5, 16))
    assert mmr_select(embeddings, [0.2, 0.4, 0.99, 0.1, 0.3], n=3)[0] == 2


def test_picks_are_unique() -> None:
    embeddings = RNG.standard_normal((10, 16))
    picks = mmr_select(embeddings, RNG.random(10), n=7)
    assert len(set(picks)) == 7


def test_asking_for_more_than_exists_returns_everything() -> None:
    embeddings = RNG.standard_normal((4, 16))
    assert sorted(mmr_select(embeddings, [1, 2, 3, 4], n=99)) == [0, 1, 2, 3]


def test_asking_for_none() -> None:
    assert mmr_select(RNG.standard_normal((4, 16)), [1, 2, 3, 4], n=0) == []


def test_identical_quality_still_selects_by_variety() -> None:
    """With a flat quality ranking the normalisation divides by zero unless it is guarded."""
    a, b = RNG.standard_normal(16), RNG.standard_normal(16)
    embeddings = np.stack([a, a + 0.01, b])
    picks = mmr_select(embeddings, [0.5, 0.5, 0.5], n=2, lam=0.7)
    assert len(picks) == 2


def test_mismatched_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="different item counts"):
        mmr_select(RNG.standard_normal((4, 8)), [1, 2, 3], n=2)


def test_lambda_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="0..1"):
        mmr_select(RNG.standard_normal((4, 8)), [1, 2, 3, 4], n=2, lam=1.5)


def test_selection_is_deterministic() -> None:
    """Golden behaviour: the same inputs must always produce the same album."""
    embeddings = np.random.default_rng(7).standard_normal((20, 32))
    quality = np.random.default_rng(8).random(20)
    first = mmr_select(embeddings, quality, n=6, lam=0.7)
    assert first == mmr_select(embeddings, quality, n=6, lam=0.7)
    # Pinned so a change in the greedy loop shows up as a failing test rather than a quietly
    # different album. Update it deliberately, never to make the suite pass.
    assert first == [1, 4, 3, 14, 19, 18]


# --- helpers --------------------------------------------------------------------------------


def test_normalise_rows_handles_a_zero_vector() -> None:
    out = normalise_rows(np.array([[3.0, 4.0], [0.0, 0.0]]))
    assert out[0].tolist() == [0.6, 0.8]
    assert out[1].tolist() == [0.0, 0.0]


def test_similarity_of_an_item_with_itself_is_one() -> None:
    similarity = similarity_matrix(RNG.standard_normal((5, 16)))
    np.testing.assert_allclose(np.diag(similarity), 1.0)


def test_quality_combines_the_terms() -> None:
    scores = quality_scores([10.0, 500.0, 90.0], [1.0, 0.5, 0.9], [0.0, 0.0, 1.0])
    assert scores[1] > scores[0], "sharper wins on the sharpness term"
    assert scores[2] > scores[1], "the face bonus outweighs a small sharpness gap"


def test_quality_uses_the_rank_of_sharpness_not_its_magnitude() -> None:
    """One enormous Laplacian variance must not swamp everything else."""
    modest = quality_scores([1.0, 2.0, 3.0], [1.0, 1.0, 1.0])
    extreme = quality_scores([1.0, 2.0, 1e6], [1.0, 1.0, 1.0])
    np.testing.assert_allclose(modest, extreme)


def test_quality_rejects_mismatched_terms() -> None:
    with pytest.raises(ValueError, match="same number of items"):
        quality_scores([1.0, 2.0], [1.0])
