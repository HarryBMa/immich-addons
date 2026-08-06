"""The probe's pure parts. It never runs against a live server in the test suite."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_probe():  # noqa: ANN202 - a module object
    spec = importlib.util.spec_from_file_location("probe", REPO_ROOT / "scripts" / "probe.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        ("/api/assets/{asset_id}", "/api/assets/{id}"),
        ("/api/albums/{album_id}/assets", "/api/albums/{id}/assets"),
        ("/api/tags/{tag_id}/assets/", "/api/tags/{tagId}/assets"),
    ],
)
def test_paths_compare_regardless_of_parameter_names(ours: str, theirs: str) -> None:
    assert probe.normalise(ours) == probe.normalise(theirs)


def test_endpoint_check_passes_against_a_matching_spec() -> None:
    from immich_addons.core.client import ENDPOINTS

    # Several paths carry more than one verb (GET and POST /api/albums), so merge rather than
    # letting a dict comprehension keep only the last one.
    paths: dict[str, dict[str, dict]] = {}
    for ep in ENDPOINTS.values():
        path = (
            ep.path.replace("{asset_id}", "{id}")
            .replace("{album_id}", "{id}")
            .replace("{tag_id}", "{id}")
        )
        paths.setdefault(path, {})[ep.verb.lower()] = {}

    checks = probe.check_endpoints({"paths": paths})
    assert [c.name for c in checks if not c.ok] == []
    assert len(checks) == len(ENDPOINTS)


def test_a_renamed_endpoint_is_reported() -> None:
    """The whole point of the probe: catch drift after an Immich upgrade."""
    spec = {"paths": {"/api/server/version": {"get": {}}}}
    failures = [c for c in probe.check_endpoints(spec) if not c.ok]
    assert failures, "a spec missing almost every path should fail"
    assert any("asset_info" in c.name for c in failures)


def test_a_verb_mismatch_is_reported() -> None:
    spec = {"paths": {"/api/server/version": {"post": {}}}}
    failure = next(c for c in probe.check_endpoints(spec) if c.name == "endpoint server_version")
    assert not failure.ok
    assert "not GET" in failure.detail


def test_additive_only_check_passes_for_the_shipped_endpoints() -> None:
    assert probe.check_destructive_paths({}).ok


def test_snapshot_is_written_to_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "CONTRACTS_DIR", tmp_path)
    monkeypatch.setattr(probe, "REPO_ROOT", tmp_path)
    dest = probe.snapshot_spec({"paths": {"/api/server/version": {}}}, "3.0.1")
    assert dest.name == "openapi-3.0.1.json"
    assert "/api/server/version" in dest.read_text(encoding="utf-8")
