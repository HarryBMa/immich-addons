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

Phase 2 — the store itself runs: catalog, per-addon config forms generated from each addon's
schema, a job queue with a live job list, and a webhook endpoint. The four addons are registered
but their pipelines are stubs, landing one phase at a time. See [PLAN.md](PLAN.md) for the phase
plan and [CLAUDE.md](CLAUDE.md) for the standing rules.

## Run it

```sh
uv sync
cp .env.example .env                # or use a vault — see docs/secrets.md
uv run immich-addons-hub            # http://127.0.0.1:8484
```

In Docker, next to an existing Immich:

```sh
docker compose -f deploy/docker-compose.yml up -d
```

The hub joins Immich's network, keeps its state in `/data`, and binds to loopback unless you widen
`HUB_BIND`. It is meant for a LAN or Tailscale — never the open internet.

## Development

```sh
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pytest           # tests — none of them touch a network or a database
```

Full setup, including the throwaway dev Immich and the fixture generator, is in
[docs/development.md](docs/development.md). Secret handling, including
[vaulted](https://github.com/woosal1337/vaulted), is in [docs/secrets.md](docs/secrets.md).

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
