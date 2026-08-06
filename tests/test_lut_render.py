"""PLAN.md §6.1 acceptance: apply real LUTs with real ffmpeg and measure the result.

Everything else in this suite mocks ffmpeg. These tests do not — they are the ones that would
catch a wrong filter graph, a misread .cube file, or an intensity blend applied backwards. They
skip when ffmpeg is missing so the rest of the suite still runs anywhere.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from immich_addons.core.media import apply_lut

pytest.importorskip("PIL")
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

REPO_ROOT = Path(__file__).resolve().parents[1]
LUTS = REPO_ROOT / "fixtures" / "luts"


def _ssim(a: np.ndarray, b: np.ndarray, *, window: int = 8) -> float:
    """Mean SSIM over non-overlapping blocks. Enough to answer "did this image survive?".

    Kept local rather than pulling in scikit-image: the project has no other use for it, and the
    block form is short enough to read and verify.
    """
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    h = (a.shape[0] // window) * window
    w = (a.shape[1] // window) * window
    blocks_a = a[:h, :w].reshape(h // window, window, w // window, window).transpose(0, 2, 1, 3)
    blocks_b = b[:h, :w].reshape(h // window, window, w // window, window).transpose(0, 2, 1, 3)
    flat_a = blocks_a.reshape(-1, window * window)
    flat_b = blocks_b.reshape(-1, window * window)

    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_a, mu_b = flat_a.mean(axis=1), flat_b.mean(axis=1)
    var_a, var_b = flat_a.var(axis=1), flat_b.var(axis=1)
    cov = ((flat_a - mu_a[:, None]) * (flat_b - mu_b[:, None])).mean(axis=1)

    numerator = (2 * mu_a * mu_b + c1) * (2 * cov + c2)
    denominator = (mu_a**2 + mu_b**2 + c1) * (var_a + var_b + c2)
    return float(np.mean(numerator / denominator))


def _grey(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as image:
        return np.asarray(image.convert("L"), dtype=np.float64)


def _channel_means(path: Path) -> tuple[float, float, float]:
    from PIL import Image

    with Image.open(path) as image:
        array = np.asarray(image.convert("RGB"), dtype=np.float64)
    return tuple(array[..., i].mean() for i in range(3))  # type: ignore[return-value]


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A photo-ish test image: a gradient plus shapes, so a LUT has tones to act on."""
    from PIL import Image, ImageDraw

    width, height = 480, 320
    array = np.zeros((height, width, 3), dtype=np.uint8)
    array[..., 0] = np.linspace(10, 245, width, dtype=np.uint8)[None, :]
    array[..., 1] = np.linspace(30, 220, height, dtype=np.uint8)[:, None]
    array[..., 2] = 128
    image = Image.fromarray(array)
    draw = ImageDraw.Draw(image)
    draw.ellipse([60, 60, 200, 200], fill=(230, 180, 90))
    draw.rectangle([280, 120, 420, 260], fill=(20, 60, 110))

    path = tmp_path_factory.mktemp("lut") / "source.png"
    image.save(path)
    return path


def test_the_shipped_luts_are_present_and_well_formed() -> None:
    for name in ("identity", "warm-film", "teal-orange"):
        path = LUTS / f"{name}.cube"
        assert path.is_file(), f"{path} missing — run scripts/make_luts.py"
        text = path.read_text(encoding="utf-8")
        assert "LUT_3D_SIZE 17" in text
        entries = [
            ln
            for ln in text.splitlines()
            if ln and not ln.startswith(("#", "TITLE", "LUT_", "DOMAIN"))
        ]
        assert len(entries) == 17**3


def test_identity_lut_at_full_intensity_leaves_the_image_alone(
    source: Path, tmp_path: Path
) -> None:
    """§6.1 acceptance: SSIM > 0.98 against the original."""
    dest = tmp_path / "identity.jpg"
    apply_lut(source, dest, LUTS / "identity.cube", intensity=1.0)
    assert _ssim(_grey(source), _grey(dest)) > 0.98


def test_zero_intensity_is_a_no_op_even_with_a_strong_lut(source: Path, tmp_path: Path) -> None:
    """The blend runs, but at zero opacity — proof the graded layer is composited over the base
    and not the other way round."""
    dest = tmp_path / "zero.jpg"
    apply_lut(source, dest, LUTS / "teal-orange.cube", intensity=0.0)
    assert _ssim(_grey(source), _grey(dest)) > 0.98


def test_warm_film_lifts_the_shadows(source: Path, tmp_path: Path) -> None:
    dest = tmp_path / "warm.jpg"
    apply_lut(source, dest, LUTS / "warm-film.cube", intensity=1.0)
    before, after = _grey(source), _grey(dest)
    assert after.min() > before.min(), "the LUT should lift the darkest tone"
    assert _ssim(before, after) < 0.999, "the LUT should visibly change the image"


def test_teal_orange_shifts_the_channel_balance(source: Path, tmp_path: Path) -> None:
    dest = tmp_path / "teal.jpg"
    apply_lut(source, dest, LUTS / "teal-orange.cube", intensity=1.0)
    r0, _, b0 = _channel_means(source)
    r1, _, b1 = _channel_means(dest)
    assert (r1 - b1) > (r0 - b0), "red should gain on blue overall"


def test_intensity_scales_the_effect(source: Path, tmp_path: Path) -> None:
    full = tmp_path / "full.jpg"
    half = tmp_path / "half.jpg"
    apply_lut(source, full, LUTS / "warm-film.cube", intensity=1.0)
    apply_lut(source, half, LUTS / "warm-film.cube", intensity=0.5)

    base = _grey(source)
    assert _ssim(base, _grey(half)) > _ssim(base, _grey(full)), "half strength should stay closer"


def test_the_source_file_is_never_modified(source: Path, tmp_path: Path) -> None:
    before = source.read_bytes()
    apply_lut(source, tmp_path / "out.jpg", LUTS / "warm-film.cube", intensity=1.0)
    assert source.read_bytes() == before
