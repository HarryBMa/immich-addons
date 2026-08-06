"""The catalog: ``registry/index.json`` cross-checked against installed entry points.

The registry is metadata; the entry points are code. Keeping them separate is what makes this a
*store* rather than a hardcoded list — a registry row whose entry point is not installed renders as
"not installed", which is exactly how a third-party addon would appear before you install it.

Per-addon state (enabled, saved config) lives in JSON files under ``$DATA_DIR/config`` so it
survives a container rebuild and can be read and edited without the hub running.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from immich_addons.addons.base import CAPABILITIES, Addon
from immich_addons.core.config import Settings, get_settings

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "immich_addons.addons"

#: Where to look for the catalog, in order: an explicit override, the repo checkout, the image.
REGISTRY_CANDIDATES: tuple[Path, ...] = (
    Path(__file__).resolve().parents[3] / "registry" / "index.json",
    Path("/app/registry/index.json"),
)


def registry_path() -> Path:
    """Resolve the registry file. ``REGISTRY_PATH`` in the environment wins."""
    override = os.environ.get("REGISTRY_PATH")
    if override:
        return Path(override)
    for candidate in REGISTRY_CANDIDATES:
        if candidate.exists():
            return candidate
    return REGISTRY_CANDIDATES[0]


class RegistryError(RuntimeError):
    """The registry file is missing or malformed."""


@dataclass
class CatalogEntry:
    """One card in the catalog: what the registry claims, plus whether the code is really there."""

    id: str
    name: str
    version: str
    description: str
    entrypoint: str
    capabilities: tuple[str, ...]
    min_immich: str
    addon: Addon | None = None
    load_error: str = ""
    registry_schema: dict[str, Any] | None = None

    @property
    def installed(self) -> bool:
        return self.addon is not None

    @property
    def status(self) -> str:
        """``load_error`` carries the detail; the status itself stays user-facing."""
        return "installed" if self.addon is not None else "not installed"

    def config_schema(self) -> dict[str, Any]:
        """Prefer the schema generated from the addon's pydantic model; fall back to the registry's
        copy so a not-installed addon can still show what it would ask for."""
        if self.addon is not None:
            return type(self.addon).config_schema()
        return self.registry_schema or {"type": "object", "properties": {}}

    def default_config(self) -> dict[str, Any]:
        return type(self.addon).default_config() if self.addon else {}


def load_registry(path: Path | None = None) -> list[dict[str, Any]]:
    """Read ``registry/index.json``."""
    path = path or registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry not found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry at {path} is not valid JSON: {exc}") from exc

    if data.get("registry_version") != 1:
        raise RegistryError(f"unsupported registry_version {data.get('registry_version')!r}")
    addons = data.get("addons", [])
    if not isinstance(addons, list):
        raise RegistryError("registry 'addons' must be a list")
    return addons


def installed_addons() -> dict[str, Addon]:
    """Instantiate every addon advertised on the ``immich_addons.addons`` entry-point group."""
    found: dict[str, Addon] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = ep.load()
            addon = cls()
        except Exception:  # noqa: BLE001 - one broken addon must not hide the rest
            log.exception("failed to load addon entry point %s", ep.name)
            continue
        found[addon.id or ep.name] = addon
    return found


def build_catalog(path: Path | None = None) -> list[CatalogEntry]:
    """Join the registry rows with the installed entry points."""
    available = installed_addons()
    entries: list[CatalogEntry] = []

    for row in load_registry(path):
        addon = available.get(row["id"])
        load_error = ""
        if addon is None:
            load_error = f"entry point {row.get('entrypoint', '?')} is not installed"
        else:
            declared = set(row.get("capabilities", []))
            actual = set(addon.capabilities)
            if declared != actual:
                log.warning(
                    "capability mismatch for %s: registry says %s, code says %s",
                    row["id"],
                    sorted(declared),
                    sorted(actual),
                )
            unknown = actual - set(CAPABILITIES)
            if unknown:
                log.warning("addon %s declares unknown capabilities %s", row["id"], sorted(unknown))

        entries.append(
            CatalogEntry(
                id=row["id"],
                name=row.get("name", row["id"]),
                version=(addon.version if addon else row.get("version", "")),
                description=row.get("description", ""),
                entrypoint=row.get("entrypoint", ""),
                capabilities=tuple(addon.capabilities if addon else row.get("capabilities", [])),
                min_immich=row.get("min_immich", ""),
                addon=addon,
                load_error=load_error,
                registry_schema=row.get("config_schema"),
            )
        )
    return entries


class AddonStore:
    """Enabled flag and saved config per addon, as one JSON file each under ``$DATA_DIR/config``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.dir = self.settings.data_dir / "config"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, addon_id: str) -> Path:
        safe = addon_id.replace("/", "_").replace("..", "_")
        return self.dir / f"{safe}.json"

    def read(self, addon_id: str) -> dict[str, Any]:
        path = self._path(addon_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.exception("config for %s is corrupt; treating it as empty", addon_id)
            return {}

    def write(self, addon_id: str, state: dict[str, Any]) -> None:
        tmp = self._path(addon_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self._path(addon_id))

    def config(self, addon_id: str) -> dict[str, Any]:
        return self.read(addon_id).get("config", {})

    def save_config(self, addon_id: str, config: dict[str, Any]) -> None:
        state = self.read(addon_id)
        state["config"] = config
        self.write(addon_id, state)

    def is_enabled(self, addon_id: str) -> bool:
        return bool(self.read(addon_id).get("enabled", False))

    def set_enabled(self, addon_id: str, enabled: bool) -> None:
        state = self.read(addon_id)
        state["enabled"] = enabled
        self.write(addon_id, state)
