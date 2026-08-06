# Development

## Run the hub locally

```sh
uv sync
cp .env.example .env        # or use a vault, see docs/secrets.md
uv run immich-addons-hub    # http://127.0.0.1:8484
```

The hub boots with an unconfigured `.env` on purpose — it shows what is missing rather than
crashing. `HUB_PASSWORD` left empty means **no login at all**, and the catalog says so in a banner.

## The dev Immich stack

The stack is disposable and deliberately isolated: its own compose project name, network, volumes
and ports, because it usually shares a host with a production Immich.

```sh
cp deploy/dev/.env.example deploy/dev/.env
docker compose -f deploy/dev/docker-compose.yml --env-file deploy/dev/.env up -d
```

| Service | Port on the host | Notes |
| --- | --- | --- |
| Immich | `127.0.0.1:2284` | production Immich keeps 2283 |
| Hub | `127.0.0.1:8485` | production hub keeps 8484 |
| Postgres | `127.0.0.1:5433` | exposed so `probe.py` can introspect from the workstation |

Then, in the dev Immich UI: create an account, create an API key, and put it in
`deploy/dev/.env` as `IMMICH_API_KEY`.

Check the image tags in `deploy/dev/.env` against
[the Immich releases](https://github.com/immich-app/immich/releases) before the first run — the
database image in particular has changed name across versions.

### Fixtures

```sh
uv run python scripts/make_fixtures.py    # generates fixtures/photos and fixtures/clips
uv run python scripts/seed_dev.py         # uploads them to the dev Immich
```

The fixtures are synthesised rather than vendored, so the generator is what lives in git (the
output is gitignored). It produces what the acceptance criteria actually need: five-frame bursts
with exactly one sharp keeper, mixed orientations, twelve distinct scenes dated one per month, and
three short clips.

`seed_dev.py` refuses to upload to a URL that does not look like a development instance.

### Verify

```sh
uv run python scripts/probe.py
```

Everything must be PASS before an addon phase is considered done.

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

No test touches a network or a database: the Immich client is exercised through
`httpx.MockTransport`, ffmpeg through a mocked `subprocess.run`, and the hub through FastAPI's
`TestClient`. If a test needs a live server, it belongs in an acceptance checklist, not in pytest.

## Adding an addon

1. Subclass `Addon` with a `config_model` (a subclass of `AddonConfig`, so `dry_run` comes along).
2. Register it in `pyproject.toml` under `[project.entry-points."immich_addons.addons"]`.
3. Add the matching row to `registry/index.json`.
4. `uv sync` to re-install the entry points.

The config form, its validation, and the job plumbing all come from those declarations — there is
no per-addon UI code.
