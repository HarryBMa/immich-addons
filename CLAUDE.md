# CLAUDE.md — standing rules

A self-hosted addon store ("hub") for Immich: a FastAPI service that reads and writes Immich only
through its REST API, plus four first-party addons (auto-lut, trip-best-picks, zine-maker,
year-highlights). Runs as one container on the NAS next to Immich; the full architecture, phase
plan, and acceptance criteria live in [PLAN.md](PLAN.md) — read it before starting any phase.

## Library safety (non-negotiable — this is an irreplaceable family photo library)

- Develop against the **dev Immich instance** (deploy/dev/). The production `.env` is only used for
  final verification, dry-run first.
- The hub's Postgres credentials are a **read-only role**. Never execute INSERT/UPDATE/DELETE/DDL
  against Immich's database.
- Via the API: never call delete endpoints, never modify original assets, never touch assets that
  lack our `addon:*` tags except to *read* them.
- Every upload: tagged `addon:<name>`, described in the job log, and traceable (deterministic
  filenames like `{addon}_{source-asset-id}_{hash}.jpg`).
- Every addon has `dry_run: true` by default. Dry-run performs the full pipeline but logs intended
  API writes instead of executing them.
- Guard against feedback loops: before processing any asset, skip it if it carries any `addon:*` tag
  or matches our upload filename pattern.

## Code standards

- Python 3.12, `uv` for env + deps, `ruff` (lint + format), `pytest`, type hints everywhere,
  `pydantic` v2 for all models/config, `httpx` for HTTP.
- Single installable package `immich_addons` with subpackages `core/`, `hub/`, `addons/`. Addons
  register via the entry-point group `immich_addons.addons`.
- Secrets live in `.env` (gitignored); `.env.example` is committed and complete.
- Small, focused commits (conventional commits). One phase per branch: `phase/0-scaffold`,
  `phase/3-auto-lut`, …
- Every phase ends with `ruff check`, `pytest`, and the phase's acceptance checks passing.

## Schema truth

- **Never trust docs, blog posts, or training data for Immich endpoint/table names** — they have
  changed across versions (e.g. table renames). The running server is the source of truth:
  introspect `information_schema` for the embedding table/column, and fetch the live OpenAPI spec
  from the server for endpoints. `scripts/probe.py` (Phase 1) automates this; run it whenever
  something 404s.
- Record `IMMICH_VERSION` in `.env`; snapshot the server's OpenAPI JSON into `contracts/` so API
  drift shows up in diffs after Immich upgrades.

## Environment notes

- Development happens on a Windows workstation; Docker stacks — including the throwaway dev Immich
  from Phase 2 — run on the NAS over SSH, not locally. The host alias, credentials and paths live in
  `.env` and in the operator's own notes, never in this repo.
- The dev stack shares a host with a production Immich. Give it its own compose project name,
  volumes, network and ports, and point `.env` at it explicitly — never let a dev run inherit
  production connection details.
