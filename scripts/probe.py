#!/usr/bin/env python
"""Verify this repo's assumptions against a running Immich server.

The running server is the source of truth for endpoint and table names (CLAUDE.md, "Schema truth").
Run this after any Immich upgrade, and whenever something 404s:

    uv run python scripts/probe.py            # probe, print PASS/FAIL
    uv run python scripts/probe.py --no-db    # skip the Postgres checks

Exit code is 0 only when every check passed. The OpenAPI document is snapshotted into
``contracts/`` so API drift shows up as a diff in the next commit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # allow `python scripts/probe.py` without installing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from immich_addons.core import db as db_mod  # noqa: E402
from immich_addons.core.client import ENDPOINTS, READ_ONLY_POSTS, ImmichClient  # noqa: E402
from immich_addons.core.config import MissingSettingError, get_settings  # noqa: E402

CONTRACTS_DIR = REPO_ROOT / "contracts"
_PATH_PARAM = re.compile(r"\{[^}]+\}")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def render(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}" + (
            f"\n       {self.detail}" if self.detail else ""
        )


def normalise(path: str) -> str:
    """``/api/assets/{asset_id}/original`` and ``/api/assets/{id}/original`` compare equal."""
    return _PATH_PARAM.sub("{}", path.rstrip("/"))


def check_endpoints(spec: dict[str, Any]) -> list[Check]:
    spec_paths = {normalise(p): p for p in spec.get("paths", {})}
    checks: list[Check] = []
    for name, ep in ENDPOINTS.items():
        wanted = normalise(ep.path)
        actual = spec_paths.get(wanted)
        if actual is None:
            checks.append(Check(f"endpoint {name}", False, f"{ep.verb} {ep.path} not in the spec"))
            continue
        verbs = {v.upper() for v in spec["paths"][actual] if v.lower() != "parameters"}
        if ep.verb not in verbs:
            checks.append(
                Check(
                    f"endpoint {name}",
                    False,
                    f"{actual} exists but offers {sorted(verbs)}, not {ep.verb}",
                )
            )
            continue
        kind = "read" if not ep.writes else "write"
        if name in READ_ONLY_POSTS:
            kind = "read (POST)"
        checks.append(Check(f"endpoint {name}", True, f"{ep.verb} {actual} — {kind}"))
    return checks


def check_destructive_paths(spec: dict[str, Any]) -> Check:
    """Confirm the client cannot reach a delete endpoint: none of our paths offer DELETE to us."""
    offenders = [
        name
        for name, ep in ENDPOINTS.items()
        if ep.verb not in {"GET", "POST", "PUT"}  # pragma: no cover - guarded at definition
    ]
    return Check(
        "client is additive only",
        not offenders,
        "every ENDPOINTS entry is GET/POST/PUT" if not offenders else f"offenders: {offenders}",
    )


def snapshot_spec(spec: dict[str, Any], version: str) -> Path:
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = CONTRACTS_DIR / f"openapi-{version or 'unknown'}.json"
    dest.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


def probe_db() -> list[Check]:
    checks: list[Check] = []
    try:
        conn = db_mod.connect()
    except MissingSettingError as exc:
        return [Check("postgres connection", False, str(exc))]
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        return [Check("postgres connection", False, f"{type(exc).__name__}: {exc}")]

    try:
        checks.append(Check("postgres connection", True, "connected as the read-only role"))
        try:
            location = db_mod.find_embedding_column(conn)
            checks.append(Check("clip embedding table", True, location.describe()))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("clip embedding table", False, f"{type(exc).__name__}: {exc}"))

        try:
            db_mod.query(conn, "SELECT 1 WHERE false; DELETE FROM assets")
        except db_mod.ReadOnlyViolationError:
            checks.append(Check("read-only guard", True, "non-SELECT statements are refused"))
        else:  # pragma: no cover - would mean the guard regressed
            checks.append(Check("read-only guard", False, "a DELETE statement was NOT refused"))
    finally:
        conn.close()
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-db", action="store_true", help="skip the Postgres checks")
    args = parser.parse_args(argv)

    settings = get_settings()
    checks: list[Check] = []

    try:
        settings.require("immich_url", "immich_api_key")
    except MissingSettingError as exc:
        print(Check("settings", False, str(exc)).render())
        return 1

    print(f"probing {settings.immich_url}\n")

    # Reads only, so dry_run is irrelevant — but be explicit about it.
    with ImmichClient(settings, dry_run=True) as client:
        try:
            version = client.server_version()
            checks.append(Check("server version", True, version))
        except Exception as exc:  # noqa: BLE001
            print(Check("server version", False, f"{type(exc).__name__}: {exc}").render())
            return 1

        if settings.immich_version and settings.immich_version != version:
            checks.append(
                Check(
                    "IMMICH_VERSION matches",
                    False,
                    f".env says {settings.immich_version}, server reports {version} — "
                    "update .env and re-snapshot the spec",
                )
            )
        else:
            checks.append(Check("IMMICH_VERSION matches", True, version))

        try:
            spec = client.openapi_spec()
            dest = snapshot_spec(spec, version)
            checks.append(
                Check(
                    "openapi snapshot",
                    True,
                    f"{dest.relative_to(REPO_ROOT)} ({len(spec.get('paths', {}))} paths)",
                )
            )
            checks.extend(check_endpoints(spec))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("openapi snapshot", False, f"{type(exc).__name__}: {exc}"))

        checks.append(check_destructive_paths({}))

        try:
            perms = client.api_key_permissions()
            checks.append(
                Check("api key permissions", bool(perms), ", ".join(perms) or "none reported")
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("api key permissions", False, f"{type(exc).__name__}: {exc}"))

    if not args.no_db:
        checks.extend(probe_db())

    for check in checks:
        print(check.render())

    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(c.name for c in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
