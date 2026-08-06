"""Scene scoring and quota-constrained selection for year-highlights (PLAN.md §6.4).

Split out from the addon because this is the part with the interesting decisions, and it is all
pure: scene records in, chosen scene records out. The ffmpeg work that *produces* the records lives
in the addon; here there is no I/O at all.

The selection problem is not "take the highest-scoring scenes". A year of video is unevenly shot —
one birthday party can hold more good footage than three quiet months — so an unconstrained top-N
would produce a film about one afternoon. Hence quotas: per day, per asset, and a coverage pass
that guarantees every month with footage is represented before the remaining time is filled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from immich_addons.core import scoring


@dataclass
class Scene:
    """One candidate cut: a span of one asset, with its scores."""

    asset_id: str
    start_s: float
    end_s: float
    taken_at: datetime
    motion: float = 0.0
    audio: float = 0.0
    sharpness: float = 0.0
    faces: float = 0.0
    #: Cheap visual descriptor (a colour histogram) used for variety, not recognition.
    descriptor: list[float] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    @property
    def day(self) -> str:
        return self.taken_at.strftime("%Y-%m-%d")

    @property
    def month(self) -> int:
        return self.taken_at.month

    def to_json(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "taken_at": self.taken_at.isoformat(),
            "motion": self.motion,
            "audio": self.audio,
            "sharpness": self.sharpness,
            "faces": self.faces,
            "descriptor": list(self.descriptor),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Scene:
        return cls(
            asset_id=str(data["asset_id"]),
            start_s=float(data["start_s"]),
            end_s=float(data["end_s"]),
            taken_at=datetime.fromisoformat(str(data["taken_at"])),
            motion=float(data.get("motion", 0.0)),
            audio=float(data.get("audio", 0.0)),
            sharpness=float(data.get("sharpness", 0.0)),
            faces=float(data.get("faces", 0.0)),
            descriptor=list(data.get("descriptor", [])),
        )


#: How much each term contributes. Motion and audio dominate because a highlight is a *moment*:
#: something happening and someone reacting. Sharpness is a veto on unusable footage more than a
#: positive signal, and faces matter but a landscape pan is still worth keeping.
WEIGHTS = {"motion": 0.35, "audio": 0.25, "sharpness": 0.2, "faces": 0.2}


def _rank(values: np.ndarray) -> np.ndarray:
    """Rank-normalise to 0..1. Raw motion and audio magnitudes are not comparable across clips —
    a quiet indoor video and a windy beach video have different scales — so only order matters."""
    if values.size == 0:
        return values
    if values.size == 1:
        return np.ones(1)
    order = np.argsort(np.argsort(values))
    return order / (values.size - 1)


def score_scenes(scenes: list[Scene]) -> np.ndarray:
    """Combine the per-scene terms into one 0..1 score."""
    if not scenes:
        return np.zeros(0)
    terms = {
        "motion": _rank(np.array([s.motion for s in scenes])),
        "audio": _rank(np.array([s.audio for s in scenes])),
        "sharpness": _rank(np.array([s.sharpness for s in scenes])),
        "faces": np.clip(np.array([s.faces for s in scenes]), 0.0, 1.0),
    }
    return sum(WEIGHTS[name] * values for name, values in terms.items())


def descriptors(scenes: list[Scene]) -> np.ndarray:
    """Descriptor matrix for MMR. Scenes without one get a distinct near-zero row, so a missing
    descriptor never makes two scenes look identical."""
    width = max((len(s.descriptor) for s in scenes), default=0) or 8
    rows = []
    for i, scene in enumerate(scenes):
        if len(scene.descriptor) == width:
            rows.append(np.asarray(scene.descriptor, dtype=np.float64))
        else:
            row = np.zeros(width)
            row[i % width] = 1e-6
            rows.append(row)
    return np.stack(rows)


def select_scenes(
    scenes: list[Scene],
    *,
    target_length_s: float,
    clip_length_s: float = 3.0,
    max_per_day: int = 3,
    max_per_asset: int = 2,
    diversity: float = 0.7,
) -> list[Scene]:
    """Choose scenes for the film: quality, with quotas and month coverage.

    Three passes:

    1. **quota filter** — drop scenes beyond ``max_per_day`` / ``max_per_asset``, keeping the best
       of each. Without this, one long birthday recording wins the whole film;
    2. **coverage** — take the best remaining scene from every month that has footage, so a year
       film actually spans the year;
    3. **fill** — MMR over what is left until the target length is reached, so the remaining time
       goes to good *and* visually varied moments.

    Returns the chosen scenes in chronological order — the film is a year, so it runs forwards.
    """
    if not scenes:
        return []

    scores = score_scenes(scenes)
    ranked = sorted(range(len(scenes)), key=lambda i: -scores[i])

    per_day: dict[str, int] = {}
    per_asset: dict[str, int] = {}
    eligible: list[int] = []
    for index in ranked:
        scene = scenes[index]
        if per_day.get(scene.day, 0) >= max_per_day:
            continue
        if per_asset.get(scene.asset_id, 0) >= max_per_asset:
            continue
        per_day[scene.day] = per_day.get(scene.day, 0) + 1
        per_asset[scene.asset_id] = per_asset.get(scene.asset_id, 0) + 1
        eligible.append(index)

    slots = max(1, int(target_length_s / max(0.5, clip_length_s)))

    chosen: list[int] = []
    by_month: dict[int, list[int]] = {}
    for index in eligible:
        by_month.setdefault(scenes[index].month, []).append(index)
    for month in sorted(by_month):
        if len(chosen) >= slots:
            break
        chosen.append(max(by_month[month], key=lambda i: scores[i]))

    remaining = [i for i in eligible if i not in set(chosen)]
    if remaining and len(chosen) < slots:
        wanted = slots - len(chosen)
        subset = descriptors([scenes[i] for i in remaining])
        picks = scoring.mmr_select(subset, scores[remaining], wanted, lam=diversity)
        chosen.extend(remaining[p] for p in picks)

    return sorted((scenes[i] for i in chosen), key=lambda s: (s.taken_at, s.start_s))


def trim_window(scene: Scene, clip_length_s: float) -> tuple[float, float]:
    """The best ``clip_length_s`` of a scene: centred, because scene boundaries usually contain
    the camera settling or turning away."""
    if scene.duration_s <= clip_length_s:
        return scene.start_s, scene.end_s
    centre = (scene.start_s + scene.end_s) / 2
    half = clip_length_s / 2
    return centre - half, centre + half


def cache_key(asset_id: str, params: dict[str, Any]) -> str:
    """Cache identity: the asset plus the parameters that change its *analysis*.

    Deliberately narrow. Only things that alter how a scene is detected or scored belong here —
    not the target length, not the music, not the title cards, because none of those change what
    the scene analysis found. That is what lets a re-run with different music reuse everything.
    """
    payload = json.dumps({"asset": asset_id, **params}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def month_name(month: int) -> str:
    return datetime(2000, month, 1).strftime("%B")
