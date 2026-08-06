from __future__ import annotations

import json
from pathlib import Path

import pytest

from immich_addons.hub.registry import (
    AddonStore,
    RegistryError,
    build_catalog,
    installed_addons,
    load_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "registry" / "index.json"


def test_every_registry_row_has_installed_code() -> None:
    """The catalog and the entry points must agree — a typo in either shows up here."""
    entries = build_catalog(REGISTRY)
    assert len(entries) == 4
    broken = [(e.id, e.load_error) for e in entries if not e.installed]
    assert broken == []


def test_registry_ids_match_the_addon_classes() -> None:
    entries = build_catalog(REGISTRY)
    for entry in entries:
        assert entry.addon is not None
        assert entry.addon.id == entry.id
        assert entry.version == entry.addon.version


def test_registry_capabilities_match_the_code() -> None:
    rows = {row["id"]: row for row in load_registry(REGISTRY)}
    for entry in build_catalog(REGISTRY):
        assert set(rows[entry.id]["capabilities"]) == set(entry.capabilities)


def test_entry_points_expose_all_four_addons() -> None:
    assert set(installed_addons()) == {
        "auto-lut",
        "trip-best-picks",
        "zine-maker",
        "year-highlights",
    }


def test_a_row_without_code_renders_as_not_installed(tmp_path: Path) -> None:
    """Exactly how a third-party addon would appear before you install it."""
    path = tmp_path / "index.json"
    path.write_text(
        json.dumps(
            {
                "registry_version": 1,
                "addons": [
                    {
                        "id": "someone-elses-addon",
                        "name": "Someone Else's Addon",
                        "version": "9.9.9",
                        "description": "Not installed here.",
                        "entrypoint": "third_party.addon:Thing",
                        "capabilities": ["manual"],
                        "min_immich": "3.0.0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    entry = build_catalog(path)[0]
    assert entry.installed is False
    assert entry.status == "not installed"
    assert "third_party.addon:Thing" in entry.load_error
    # A not-installed addon still shows what it would ask for, from the registry's own schema.
    assert entry.config_schema() == {"type": "object", "properties": {}}


def test_unsupported_registry_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"registry_version": 2, "addons": []}), encoding="utf-8")
    with pytest.raises(RegistryError, match="registry_version"):
        load_registry(path)


def test_malformed_registry_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_registry(path)


def test_missing_registry_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "nope.json")


def test_store_round_trips_config_and_enabled(settings) -> None:  # noqa: ANN001
    store = AddonStore(settings)

    assert store.is_enabled("auto-lut") is False
    assert store.config("auto-lut") == {}

    store.save_config("auto-lut", {"intensity": 0.5})
    store.set_enabled("auto-lut", True)

    assert AddonStore(settings).config("auto-lut") == {"intensity": 0.5}
    assert AddonStore(settings).is_enabled("auto-lut") is True


def test_store_survives_a_corrupt_file(settings) -> None:  # noqa: ANN001
    store = AddonStore(settings)
    store.save_config("auto-lut", {"intensity": 0.5})
    (store.dir / "auto-lut.json").write_text("{broken", encoding="utf-8")
    assert store.config("auto-lut") == {}


def test_store_cannot_escape_its_directory(settings) -> None:  # noqa: ANN001
    store = AddonStore(settings)
    store.save_config("../../etc/passwd", {"x": 1})
    written = list(store.dir.glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == store.dir
