# immich-addons

A self-hosted addon store for [Immich](https://immich.app). One container (the *hub*) runs beside
your Immich server, presents a catalog of addons with per-addon config forms and a job queue, and
does all of its work through Immich's REST API — additively, never destructively.

First-party addons:

| Addon | What it does |
| --- | --- |
| `auto-lut` | Applies a `.cube` LUT to new photos/videos, stacked with the original. |
| `trip-best-picks` | Picks the best *and most varied* photos from a trip into an album. |
| `zine-maker` | Lays out a printable zine (single-sheet mini8 or saddle-stitch booklet) as PDF. |
| `year-highlights` | Cuts a year of video into a music-backed highlight film. |

## Status

Phase 1 — the shared core library (Immich client, read-only embedding reader, job queue, ffmpeg
wrappers) and `scripts/probe.py`. No addons implemented yet. See [PLAN.md](PLAN.md) for the phase
plan and [CLAUDE.md](CLAUDE.md) for the standing rules.

## Development

```sh
uv sync                 # create .venv and install deps + dev group from the lockfile
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pytest           # tests
```

Copy `.env.example` to `.env` and fill it in before running anything that talks to Immich.

Then check this repo's assumptions against your actual server — endpoint names and the CLIP
embedding table are discovered, never hardcoded from documentation:

```sh
uv run python scripts/probe.py           # version, OpenAPI snapshot, endpoints, key perms, DB
uv run python scripts/probe.py --no-db   # skip the Postgres checks
```

Exit code 0 means every check passed. Rerun it after every Immich upgrade and commit the resulting
snapshot under [`contracts/`](contracts/).

## Licence

MIT — see [LICENSE](LICENSE).
