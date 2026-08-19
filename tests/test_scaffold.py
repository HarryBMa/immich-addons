"""Phase 0 smoke tests: the package imports and the registry file is well-formed."""

import json
from pathlib import Path

import immich_addons

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_package_imports() -> None:
    assert immich_addons.__version__


def test_registry_index_is_valid() -> None:
    index = json.loads((REPO_ROOT / "registry" / "index.json").read_text(encoding="utf-8"))
    assert index["registry_version"] == 1
    assert index["addons"] == []
