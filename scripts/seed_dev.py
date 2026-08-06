#!/usr/bin/env python
"""Upload fixtures/ into the development Immich.

    uv run python scripts/make_fixtures.py
    uv run python scripts/seed_dev.py

Refuses to run unless ``IMMICH_URL`` points at a host that looks like a development instance
(localhost or the dev port), because "seed a few hundred synthetic photos" is not something you
want to fire at the family library by accident. Pass ``--i-know`` only if you have genuinely
pointed a throwaway server at an unusual address.

Uploads are ordinary API writes, so they honour ``--dry-run``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from immich_addons.core.client import ImmichClient  # noqa: E402
from immich_addons.core.config import MissingSettingError, get_settings  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures"
DEV_MARKERS = ("localhost", "127.0.0.1", ":2284", ":8485", "immich-server")


def looks_like_dev(url: str) -> bool:
    return any(marker in url for marker in DEV_MARKERS)


def fixture_files() -> list[Path]:
    if not FIXTURES.exists():
        return []
    return sorted(
        p
        for p in FIXTURES.rglob("*")
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="log uploads instead of sending")
    parser.add_argument(
        "--i-know",
        action="store_true",
        help="skip the development-instance check (you are certain this is not the family server)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        settings.require("immich_url", "immich_api_key")
    except MissingSettingError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not looks_like_dev(settings.immich_url) and not args.i_know:
        print(
            f"refusing to seed {settings.immich_url}: it does not look like a development "
            "instance. Point IMMICH_URL at the dev stack, or pass --i-know.",
            file=sys.stderr,
        )
        return 1

    files = fixture_files()
    if not files:
        print("no fixtures found — run scripts/make_fixtures.py first", file=sys.stderr)
        return 1

    print(f"seeding {len(files)} fixtures into {settings.immich_url}")
    with ImmichClient(settings, dry_run=args.dry_run) as client:
        for i, path in enumerate(files, start=1):
            client.upload_asset(path, device_asset_id=f"fixture_{path.stem}")
            print(f"  [{i}/{len(files)}] {path.relative_to(FIXTURES)}")

    print("done. Give Immich a minute to run smart search, then: uv run python scripts/probe.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
