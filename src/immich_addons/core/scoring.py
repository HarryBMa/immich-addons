"""Selection maths shared by trip-best-picks, zine-maker and year-highlights (PLAN.md §6).

Every function here is pure: arrays in, numbers or indices out. No Immich, no files, no I/O. That
is deliberate — this is the part where a subtle mistake produces plausible-looking but bad
selections, so it has to be testable against synthetic data where the right answer is known.

The four pieces:

* :func:`sharpness` and :func:`exposure` — cheap per-image quality terms;
* :func:`near_dup_groups` — collapses bursts, so five frames of the same moment cannot take five
  of the twenty-four slots;
* :func:`mmr_select` — the actual selection: quality *with variety*.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

#: Laplacian kernel used for the sharpness estimate.
_LAPLACIAN = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]])


def _as_grey(image: np.ndarray) -> np.ndarray:
    """Accept greyscale or RGB(A) and return a float greyscale plane in 0..255."""
    array = np.asarray(image, dtype=np.float64)
    if array.ndim == 3:
        array = array[..., :3] @ np.array([0.2126, 0.7152, 0.0722])
    if array.ndim != 2:
        raise ValueError(f"expected a 2D or 3D image array, got shape {np.shape(image)}")
    if array.max() <= 1.0 + 1e-9:
        array = array * 255.0
    return array


def _convolve2d(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Valid-mode 2D convolution via stride tricks. Small kernels only; no SciPy dependency."""
    kh, kw = kernel.shape
    if image.shape[0] < kh or image.shape[1] < kw:
        return np.zeros((0, 0))
    windows = np.lib.stride_tricks.sliding_window_view(image, (kh, kw))
    return np.einsum("ijkl,kl->ij", windows, kernel)


def sharpness(image: np.ndarray, *, long_edge: int = 720) -> float:
    """Variance of the Laplacian — the standard cheap focus measure.

    Higher is sharper. The image is downsampled so the number does not depend on resolution: a
    24 MP frame and a 12 MP frame of the same scene should score alike.
    """
    grey = _as_grey(image)
    if grey.size == 0:
        return 0.0

    longest = max(grey.shape)
    if longest > long_edge:
        step = int(np.ceil(longest / long_edge))
        grey = grey[::step, ::step]

    response = _convolve2d(grey, _LAPLACIAN)
    return float(response.var()) if response.size else 0.0


def exposure(image: np.ndarray, *, clip_low: float = 2.0, clip_high: float = 253.0) -> float:
    """1 − the fraction of pixels clipped at either end of the histogram. 1.0 is unclipped.

    Both ends matter: a blown sky and a crushed shadow are equally lost information.
    """
    grey = _as_grey(image)
    if grey.size == 0:
        return 0.0
    clipped = np.count_nonzero((grey <= clip_low) | (grey >= clip_high))
    return float(1.0 - clipped / grey.size)


def normalise_rows(embeddings: np.ndarray) -> np.ndarray:
    """L2-normalise each row so a dot product is a cosine similarity."""
    array = np.asarray(embeddings, dtype=np.float64)
    if array.ndim != 2:
        raise ValueError(f"expected a 2D array of embeddings, got shape {array.shape}")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return array / norms


def similarity_matrix(embeddings: np.ndarray) -> np.ndarray:
    """Pairwise cosine similarity."""
    unit = normalise_rows(embeddings)
    return unit @ unit.T


def near_dup_groups(embeddings: np.ndarray, thr: float = 0.96) -> list[list[int]]:
    """Group near-identical items — burst frames, or the same shot taken twice.

    Single-link clustering over the similarity graph: two items join the same group if they are
    similar to each other *or* to something already in it. That is what makes a burst where each
    frame drifts slightly from the last still collapse into one group.

    Returns groups of indices, each group sorted, groups ordered by their first member. Every index
    appears exactly once, so singletons come back as one-element groups.
    """
    array = np.asarray(embeddings, dtype=np.float64)
    if array.size == 0:
        return []
    n = array.shape[0]
    similarity = similarity_matrix(array)

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i, j in zip(*np.where(np.triu(similarity >= thr, k=1)), strict=True):
        union(int(i), int(j))

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [sorted(members) for _, members in sorted(groups.items())]


def collapse_bursts(
    embeddings: np.ndarray, quality: Sequence[float], thr: float = 0.96
) -> list[int]:
    """Keep the highest-quality member of each near-duplicate group. Returns kept indices."""
    scores = np.asarray(quality, dtype=np.float64)
    kept = [max(group, key=lambda i: scores[i]) for group in near_dup_groups(embeddings, thr)]
    return sorted(kept)


def mmr_select(
    embeddings: np.ndarray,
    quality: Sequence[float],
    n: int,
    lam: float = 0.7,
) -> list[int]:
    """Maximal marginal relevance: the best items that are also unlike each other.

    Greedy. Each step picks the item maximising::

        lam * quality[i] - (1 - lam) * max(similarity(i, already_picked))

    ``lam = 1`` ignores variety and just takes the top of the quality ranking; ``lam = 0`` ignores
    quality and spreads as widely as possible. The default 0.7 leans on quality while refusing to
    fill the album with one afternoon.

    Quality is min-max normalised first, so the two terms are on the same 0..1 scale — otherwise
    ``lam`` would mean something different for every scoring scheme.
    """
    array = np.asarray(embeddings, dtype=np.float64)
    scores = np.asarray(quality, dtype=np.float64)
    if array.ndim != 2 or array.shape[0] != scores.shape[0]:
        raise ValueError(
            f"embeddings {array.shape} and quality {scores.shape} describe different item counts"
        )
    if not 0.0 <= lam <= 1.0:
        raise ValueError(f"lam must be within 0..1, got {lam}")

    count = array.shape[0]
    n = min(max(int(n), 0), count)
    if n == 0:
        return []

    spread = scores.max() - scores.min()
    normalised = (scores - scores.min()) / spread if spread > 0 else np.zeros_like(scores)

    similarity = similarity_matrix(array)
    picked: list[int] = [int(np.argmax(normalised))]
    # Running max similarity to the picked set, so each step is O(count) rather than O(count·k).
    max_sim = similarity[picked[0]].copy()

    while len(picked) < n:
        objective = lam * normalised - (1.0 - lam) * max_sim
        objective[picked] = -np.inf
        choice = int(np.argmax(objective))
        picked.append(choice)
        np.maximum(max_sim, similarity[choice], out=max_sim)

    return picked


def quality_scores(
    sharpness_values: Sequence[float],
    exposure_values: Sequence[float],
    face_bonus: Sequence[float] | None = None,
    *,
    weights: tuple[float, float, float] = (0.5, 0.3, 0.2),
) -> np.ndarray:
    """Combine the per-image terms into one 0..1 quality score.

    Sharpness is rank-normalised rather than min-max scaled: its raw range depends on the scene
    (a picture of a brick wall out-scores a portrait), so only the ordering is meaningful.
    """
    sharp = np.asarray(sharpness_values, dtype=np.float64)
    exposed = np.asarray(exposure_values, dtype=np.float64)
    faces = np.zeros_like(sharp) if face_bonus is None else np.asarray(face_bonus, dtype=np.float64)
    if not (sharp.shape == exposed.shape == faces.shape):
        raise ValueError("quality terms must all describe the same number of items")
    if sharp.size == 0:
        return sharp

    order = np.argsort(np.argsort(sharp))
    sharp_rank = order / (len(order) - 1) if len(order) > 1 else np.ones_like(sharp)

    ws, we, wf = weights
    return ws * sharp_rank + we * np.clip(exposed, 0.0, 1.0) + wf * np.clip(faces, 0.0, 1.0)
