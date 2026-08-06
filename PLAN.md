# immich-addons — Build Plan

> **Repo:** `github.com/HarryBMa/immich-addons` (currently empty — greenfield)
> **Executor:** Claude Code, one phase per session. Harry reviews & merges each phase.
> **Target host:** NAS — Intel i3-N305, Ubuntu Server, Docker/Portainer, already running Immich v3.x (+ Postgres, Redis)
> **Goal:** a self-hosted addon store ("hub") for Immich + four addons: **auto-lut**, **trip-best-picks**, **zine-maker**, **year-highlights**

## How to use this plan

1. Commit this file as `PLAN.md` in the repo root.
2. Run **Phase 0** first — it writes `CLAUDE.md` (the standing rules Claude Code re-reads every session).
3. One phase = one branch = one Claude Code session. Paste the phase prompt verbatim.
4. Gate before merging: acceptance checks green **and** the "Understand it" checklist done (you read the listed code and can explain it).
5. Never point development at the family Immich instance. Phase 2 sets up a throwaway dev Immich with fixture photos; the real instance is only used at the very end of each addon phase, in dry-run mode first.

---

## 1. Architecture

```
                     ┌──────────────────────────────────┐
 Immich web/mobile ──►  Immich server v3  (:2283)       │
    ▲                │                                  │
    │ albums, tags,  │  Workflows (preview):            │
    │ stacks, assets │  trigger → filter → webhook ─────┼───┐
    │ appear here    └───────────────┬──────────────────┘   │
    │                                │ REST API (all writes)│ POST /hooks/immich
    │                                ▼                      ▼
    │                 ┌────────────────────────────────────────┐
    └─────────────────┤  addons-hub  (this repo, :8484)        │
                      │  FastAPI · Jinja2 + htmx catalog UI    │
                      │  registry/index.json → addon catalog   │
                      │  job queue (SQLite) · webhook dispatch │
                      │  addons: auto-lut · trip-best-picks    │
                      │          zine-maker · year-highlights  │
                      └────────┬──────────────┬────────────────┘
                               │ SELECT only  │ ffmpeg (QSV /dev/dri),
                               ▼              ▼ ONNX (CPU), WeasyPrint
                      Immich Postgres     /data volume
                      (CLIP embeddings)   (luts, music, output, cache)
```

### Key decisions (and why)

1. **Companion-service architecture, not Wasm plugins (for v1).** Immich v3's new plugin system runs sandboxed WebAssembly (Extism) *inside* the server — the right tool for filters and tagging steps, the wrong tool for ffmpeg, ONNX inference, and PDF rendering. The officially supported escape hatch is the Workflows **webhook** step, which calls out to our hub. Native Wasm steps are backlog (§9), not v1.
2. **One hub container; addons are Python packages discovered via entry points.** The "store" = `registry/index.json` (metadata, config schemas) + a catalog UI with enable/disable + config forms. This teaches real plugin architecture without the docker-socket security tradeoffs of multi-container installs (backlog).
3. **All writes go through the Immich REST API.** Direct Postgres access is a **read-only** role, used only to read CLIP embeddings. The hub never writes to Immich's DB. Ever.
4. **Everything is reversible.** Every asset the hub uploads carries tag `addon:<name>`; dry-run is the default for every addon until explicitly disabled; nothing is ever deleted or overwritten.
5. **Workflows is a preview feature — assume it will break.** Webhook handling stays thin, and every event-driven addon also supports `TRIGGER_MODE=poll` (cursor-based "assets since last check" every N minutes) as a fallback that only depends on the stable REST API.
6. **In-app UI = a thin fork of Immich. UI-only, always optional.** A patch series of exactly three small commits on top of upstream **release tags** adds: a sidebar panel that iframes the hub, a "Send to Addons" action on selections/albums, and the config/CSP plumbing (Phase 8). Hard rule: the hub must work 100% against *stock* Immich — the fork adds convenience, never capability — so the family instance can drop back to upstream images at any moment. The patches are written as a generic, config-driven **"External apps"** feature (nothing hub-specific hardcoded) so they double as an upstream proposal; if Immich ever ships an official addon-store spot, the fork dissolves into it.

---

## 2. Standing rules → `CLAUDE.md`

Phase 0 writes a `CLAUDE.md` containing exactly these rules. They apply to every phase.

### Library safety (non-negotiable — this is an irreplaceable family photo library)

- Develop against the **dev Immich instance** (deploy/dev/). The production `.env` is only used for final verification, dry-run first.
- The hub's Postgres credentials are a **read-only role**. Never execute INSERT/UPDATE/DELETE/DDL against Immich's database.
- Via the API: never call delete endpoints, never modify original assets, never touch assets that lack our `addon:*` tags except to *read* them.
- Every upload: tagged `addon:<name>`, described in the job log, and traceable (deterministic filenames like `{addon}_{source-asset-id}_{hash}.jpg`).
- Every addon has `dry_run: true` by default. Dry-run performs the full pipeline but logs intended API writes instead of executing them.
- Guard against feedback loops: before processing any asset, skip it if it carries any `addon:*` tag or matches our upload filename pattern.

### Code standards

- Python 3.12, `uv` for env + deps, `ruff` (lint + format), `pytest`, type hints everywhere, `pydantic` v2 for all models/config, `httpx` for HTTP.
- Single installable package `immich_addons` with subpackages `core/`, `hub/`, `addons/`. Addons register via the entry-point group `immich_addons.addons`.
- Secrets live in `.env` (gitignored); `.env.example` is committed and complete.
- Small, focused commits (conventional commits). One phase per branch: `phase/0-scaffold`, `phase/3-auto-lut`, …
- Every phase ends with `ruff check`, `pytest`, and the phase's acceptance checks passing.

### Schema truth

- **Never trust docs, blog posts, or training data for Immich endpoint/table names** — they have changed across versions (e.g., table renames). The running server is the source of truth: introspect `information_schema` for the embedding table/column, and fetch the live OpenAPI spec from the server for endpoints. `scripts/probe.py` (Phase 1) automates this; run it whenever something 404s.
- Record `IMMICH_VERSION` in `.env`; snapshot the server's OpenAPI JSON into `contracts/` so API drift shows up in diffs after Immich upgrades.

---

## 3. Repo layout (target state)

```
immich-addons/
├── CLAUDE.md                    # standing rules (Phase 0)
├── PLAN.md                      # this file
├── README.md
├── pyproject.toml               # uv-managed, single package
├── .env.example
├── LICENSE                      # MIT
├── Dockerfile                   # hub image: python:3.12-slim + ffmpeg, exiftool, WeasyPrint deps
├── .github/workflows/ci.yml     # ruff + pytest on every push
├── registry/
│   └── index.json               # addon catalog metadata (§5)
├── src/immich_addons/
│   ├── core/                    # shared library (Phase 1)
│   │   ├── client.py            # typed Immich REST client (httpx)
│   │   ├── db.py                # read-only embedding reader
│   │   ├── config.py            # pydantic settings (.env)
│   │   ├── jobs.py              # SQLite job queue + worker thread
│   │   ├── media.py             # ffmpeg/ffprobe wrappers (QSV detect)
│   │   └── scoring.py           # sharpness, exposure, near-dup, MMR
│   ├── hub/                     # FastAPI app + UI (Phase 2)
│   │   ├── app.py               # routes: catalog, jobs, hooks, api
│   │   ├── registry.py          # index.json + entry-point loader
│   │   └── templates/           # Jinja2 + htmx
│   └── addons/
│       ├── base.py              # Addon protocol (run(), on_event(), schema)
│       ├── auto_lut/            # Phase 3
│       ├── trip_best_picks/     # Phase 4
│       ├── zine_maker/          # Phase 5
│       └── year_highlights/     # Phase 6
├── scripts/
│   ├── probe.py                 # verify server version, endpoints, embedding table
│   └── seed_dev.py              # push fixtures into the dev Immich
├── deploy/
│   ├── docker-compose.yml       # hub for production NAS (joins immich network)
│   └── dev/docker-compose.yml   # throwaway Immich + hub for development
├── contracts/                   # snapshotted OpenAPI spec per Immich version
├── fixtures/                    # ~30 CC0 photos + 3 short clips (Phase 2)
└── tests/
```

---

## 4. Immich integration contract

### Environment (`.env.example`)

```
IMMICH_URL=http://immich-server:2283
IMMICH_API_KEY=            # created in Immich → Account Settings → API Keys
IMMICH_VERSION=            # e.g. 3.0.1 — probe.py fills/checks this
IMMICH_DB_HOST=            # Postgres host (read-only role, §4 below)
IMMICH_DB_PORT=5432
IMMICH_DB_NAME=immich
IMMICH_DB_USER=addons_ro
IMMICH_DB_PASSWORD=
HUB_PORT=8484
HUB_PASSWORD=              # single shared login; hub is LAN/Tailscale-only
EMBED_ORIGIN=              # Immich web origin allowed to iframe the hub + call /api/inbox (Phase 8)
DATA_DIR=/data             # luts/, music/, output/, cache/, jobs.sqlite
TRIGGER_MODE=webhook       # webhook | poll
OLLAMA_URL=                # optional, e.g. http://gaming-pc:11434 (zine captions)
DRY_RUN_DEFAULT=true
```

### API usage (verified by `probe.py` against the live OpenAPI spec)

Read: smart search (CLIP text query), metadata search (date/type/person filters), asset info + people/faces, original & thumbnail download.
Write: asset upload, album create/add, tag create/assign, stack create.
Create the API key with the narrowest permission set Immich offers that covers exactly these (Immich has granular API-key permissions; probe.py lists what the key can do).

### Read-only Postgres role (run once, manually, on the NAS)

```sql
CREATE ROLE addons_ro LOGIN PASSWORD '<generate>';
GRANT CONNECT ON DATABASE immich TO addons_ro;
GRANT USAGE ON SCHEMA public TO addons_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO addons_ro;
-- deliberately no ALTER DEFAULT PRIVILEGES beyond SELECT; no write grants of any kind
```

`core/db.py` locates the CLIP embedding table/column at startup by introspection (vector-typed column referencing assets) rather than hardcoding names, and exposes exactly one function: `embeddings_for(asset_ids) -> dict[str, np.ndarray]`.

### Workflow wiring (done in the Immich admin UI, documented in Phase 3)

1. Admin → Workflows → new workflow. Trigger: **AssetCreate**.
2. Steps: core filter (e.g., filename/extension) → **webhook** → `http://addons-hub:8484/hooks/immich`.
3. Add the hub host to the plugin **allowed hosts** setting (v3 restricts plugin outbound HTTP targets).
4. Phase 3's first task is to log the raw webhook payload and build the parser from *observed* payloads — the shape is a preview feature and may change between releases. If it breaks after an upgrade: flip `TRIGGER_MODE=poll` and file an issue to adapt.

---

## 5. The store itself (hub spec)

### `registry/index.json` — one entry per addon

```json
{
  "registry_version": 1,
  "addons": [
    {
      "id": "auto-lut",
      "name": "Auto-LUT",
      "version": "0.1.0",
      "description": "Applies a chosen .cube LUT to new photos/videos and stacks the graded copy with the original.",
      "entrypoint": "immich_addons.addons.auto_lut:AutoLut",
      "capabilities": ["webhook", "poll"],
      "min_immich": "3.0.0",
      "config_schema": { "$ref": "see addon spec — JSON Schema, drives the UI form" }
    }
  ]
}
```

`capabilities` ∈ `webhook` (reacts to Immich events), `manual` (run from hub UI with parameters), `schedule` (cron-style). The hub cross-checks `entrypoint` against installed entry points; a registry row without code renders as "not installed" — which is exactly how third-party addons would appear later (§9).

### Hub pages & endpoints

- `/` **Catalog** — one card per registry entry: name, version, description, enabled toggle, Configure, Run (if `manual`), health dot.
- `/addons/{id}` — config form **auto-generated from the addon's JSON Schema** (strings, numbers, booleans, enums, file-pickers for `/data` paths). Saving validates through pydantic. This schema→form generator is the single most reusable thing in the hub.
- `/jobs` — job list with live status via htmx polling (2 s): queued/running/done/failed, progress %, last log lines, produced artifacts (files under `/data/output` + links to created Immich albums).
- `POST /hooks/immich` — webhook dispatcher: verify a shared secret with a constant-time compare — custom header if Immich's webhook step supports headers, otherwise a secret path token (`/hooks/immich/{token}`); never a query param, and the hub must not log the token. Parse the event, fan out to enabled addons whose capabilities include `webhook`. Always 200-fast; real work goes to the job queue.
- `POST /api/run/{id}` — enqueue a manual run with a params payload (the UI form posts here).
- Auth: one shared password (session cookie); rate-limit `/hooks/*`. The hub binds to the LAN/Tailscale only — it is never exposed to the internet.

### Job queue (`core/jobs.py`)

SQLite table (`id, addon, params_json, status, progress, log, created_at, artifacts_json`) + a single background worker thread. Jobs write progress via a callback. On startup, anything left `running` is marked `failed (interrupted)` — except addons that implement resume markers (year-highlights does, §6.4). Simple by design; a Redis/arq upgrade is backlog.

---

## 6. Addon specs

Shared pipeline utilities live in `core/scoring.py` and are built once (Phase 4) and reused by 6.2, 6.3, 6.4:

- `sharpness(img)` — variance of Laplacian on a ~720 px thumbnail.
- `exposure(img)` — 1 − fraction of clipped pixels (both ends of the histogram).
- `near_dup_groups(embs, thr=0.96)` — collapse burst shots; keep the highest-quality member of each group.
- `mmr_select(embs, quality, n, lam=0.7)` — maximal marginal relevance: greedily pick items maximizing `lam·quality − (1−lam)·max_sim_to_picked`. This is the "best photos *with variation*" core.

### 6.1 auto-lut — event-driven (webhook/poll)

**Config (JSON Schema):** `lut` (enum of `/data/luts/*.cube`), `intensity` (0–1, blend graded over original), `scope` (optional: album IDs, camera models, extensions), `process_videos` (bool, default false), `stack_original` (bool, default true), `dry_run`.

**Flow:** event → fetch asset info → skip if: already `addon:*`-tagged, matches our upload filename pattern, or outside scope → download original → apply LUT
— images: ffmpeg `lut3d` (or pillow-lut), then copy EXIF from the original with exiftool; HEIC input decodes via pillow-heif if the packaged ffmpeg lacks libheif; output JPEG q≈92; RAW is skipped with a logged reason (v1);
— videos: ffmpeg `lut3d` + `h264_qsv` (fallback `libx264` if `/dev/dri` missing)
→ upload graded copy, tag `addon:auto-lut`, stack with original → job log records before/after IDs.

**Acceptance:** unit tests apply a known LUT to fixture images and compare histograms; an identity LUT at intensity 1.0 leaves the image visually unchanged (SSIM > 0.98); dry-run logs every intended write; on the dev instance an upload triggers exactly one graded, stacked, tagged copy; re-sending the same event is a no-op (idempotent); the graded copy never re-triggers the addon.

### 6.2 trip-best-picks — manual

**Inputs (UI form):** source = album | date range | auto-detected trip; `n_picks` (default 24); `people_boost` (multi-select of Immich people; photos containing them get a quality multiplier); `embedding_source` = `db` (read from Immich's Postgres) | `local` (compute with open_clip ViT-B/32 on thumbnails, CPU — slower but zero DB dependency); `dry_run`.

**Trip auto-detection:** cluster the library's (timestamp, GPS) stream — a new trip starts on a time gap > 18 h combined with distance > 150 km from the median home location. Present detected trips as a dropdown with date ranges and photo counts.

**Pipeline:** candidates → embeddings → `near_dup_groups` → quality = weighted sharpness + exposure + face bonus (face count/area from Immich people data, boosted for `people_boost` selections) → `mmr_select(n_picks)` → output: album **"Best of {trip}"** + tag `addon:trip-best-picks`. UI shows a preview grid before committing (dry-run result = the grid), with a "re-roll" that re-runs selection at different λ.

**Acceptance:** fixture set with synthetic bursts (5 near-identical frames) collapses each burst to one pick; picks span the fixture's distinct scenes rather than clustering; a 500-photo trip completes in minutes on the N305 in `db` mode; `local` mode produces comparable picks.

### 6.3 zine-maker — manual

**Inputs:** `topic` (free-text CLIP smart-search query, e.g. *"kids at the beach"*) **or** source album; `pages` = 8 | 16; `page_orientation` = portrait | landscape; `format` = `mini8` | `booklet`; `title`; `captions` = none | exif (date + place) | ollama (optional: vision model via `OLLAMA_URL` writes one-line captions; graceful fallback to exif); `dry_run`.

**Formats — the fun part:**
- **`mini8`** — the classic single-sheet zine: 8 panels on one A4 (landscape), printed single-sided, folded three times with one center cut. Imposition: 2×4 grid, top row rotated 180°, panel order top `5 4 3 2`, bottom `6 7 8 1`, with a printed cut mark across the center of the middle columns. **Acceptance requires a physical print-fold test** — imposition bugs are invisible on screen.
- **`booklet`** — A5 saddle-stitch booklet from duplex A4 sheets (8 pp = 2 sheets, 16 pp = 4 sheets). Generic imposition for any `n % 4 == 0`: each printed side holds page pair `(i, 2N+1−i)`; side sequence `(2N,1), (2,2N−1), (2N−2,3), (4,2N−3), …`; duplex flip on short edge. Implemented as a pure function `impose(n_pages) -> list[Sheet]` with golden-file tests for n=8 and n=16.

**Pipeline:** gather ~80 candidates (smart search or album) → quality filter + `near_dup_groups` → `mmr_select` ~1.3× the needed count → sequence (chronological default; `arc` mode: cluster embeddings, order clusters, order within cluster by time) → assign per-page templates by aspect fit (full-bleed, 2-up vertical, 2-up horizontal, 4-grid, title cover, colophon back cover — templates minimize crop loss for the assigned photos' aspect ratios, which is what makes portrait *and* landscape photos both work) → render pages as HTML/CSS (Jinja2) → **WeasyPrint** → `zine_sequential.pdf` (read on screen, 300 DPI) + `zine_print.pdf` (imposed) → page-preview JPEGs in the hub UI; optional upload of page images as Immich album "Zine: {title}".

**Acceptance:** both formats × both orientations render; golden tests pass for both imposition matrices; a printed `mini8` folds correctly (human check); captions degrade gracefully when Ollama is unreachable; 300 DPI output with 3 mm bleed marks toggle.

### 6.4 year-highlights — manual + optional yearly schedule

**Inputs:** `year` (or date range); `target_length_s` (default 180); `people_filter` (only events featuring selected people); `music_file` (picker over `/data/music` — your own/licensed audio only; no music = ambient audio from clips); `include_photos` (bool: 2 s Ken Burns stills mixed in); `month_titles` (bool); `dry_run`.

**Pipeline:** fetch the year's videos (+ photos) via metadata search → per video: PySceneDetect (content detector) → candidate scenes → score = motion (mean frame-diff) + audio RMS + sharpness + face presence (sample 1 fps, small CPU ONNX face detector) → selection under quotas: max clips per day/event, coverage spread across all 12 months, `mmr_select` on scene embeddings for variety → trim the best 2–4 s of each selected scene → assemble chronologically: 0.5 s crossfades, optional month title cards, audio = music with ducking (or music only) → encode 1080p `h264_qsv` → upload to album **"Highlights {year}"**, tag `addon:year-highlights`.

**Runtime reality:** a full year on the N305 is an overnight job. Therefore: per-scene analysis results and trimmed segments are cached in `/data/cache` keyed by asset ID + params hash, and the job resumes from cache after interruption instead of restarting. QSV via `/dev/dri` passthrough; `libx264` fallback.

**Acceptance:** a 10-minute fixture corpus produces a coherent ~60 s film with clips from every fixture "month"; kill the container mid-render → rerun resumes from cache; originals are never modified; the same params produce the same film (deterministic given cache).

---

## 7. Phases — paste these prompts into Claude Code

### Phase 0 — Scaffold & standing rules
**Goal:** empty repo → working skeleton + `CLAUDE.md`.

> **Prompt:** Read PLAN.md fully. Create the repo skeleton exactly per §3: uv-managed `pyproject.toml` for a single package `immich_addons` (py3.12), ruff + pytest configured, `.gitignore`, `.env.example` per §4, empty `registry/index.json` (registry_version only), `README.md` stub, GitHub Actions workflow running ruff + pytest. Write `CLAUDE.md` containing exactly the rules in PLAN.md §2, plus a two-line project summary and a pointer back to PLAN.md. Do not implement any features. Finish: run `ruff check` and `pytest` (empty suite passing), print the tree.

**Acceptance:** CI green on the branch; `uv sync && pytest` works locally.
**Understand it:** read `pyproject.toml` and `CLAUDE.md`; be able to say what `uv sync` does and where deps are pinned.

### Phase 1 — Core library + probe
**Goal:** everything addons share; verified against a live server.

> **Prompt:** Read CLAUDE.md and PLAN.md §3–4. Implement `core/config.py` (pydantic-settings from .env), `core/client.py` (typed httpx Immich client covering exactly the endpoints in §4, with a `dry_run` mode that logs write calls instead of sending them), `core/db.py` (read-only embedding reader with introspection per §4), `core/jobs.py` (SQLite queue + worker per §5), `core/media.py` (ffprobe/ffmpeg wrappers with QSV detection and libx264 fallback). Implement `scripts/probe.py`: prints server version, fetches and snapshots the OpenAPI spec into `contracts/`, checks each endpoint we use exists, verifies API-key permissions, locates the embedding table, and prints a PASS/FAIL summary. Unit tests with mocked httpx; no live calls in tests.

**Acceptance:** `pytest` green; a test asserts the client class exposes **no** delete or modify-original methods (the additive-only rule as executable code); `probe.py` passes against the dev instance (Phase 2) — rerun it here once Phase 2 exists.
**Understand it:** read `client.py` top to bottom; explain how the API key header is injected and what dry-run interception does; run `probe.py` and read every line of output.

### Phase 2 — Hub app + dev environment
**Goal:** the store shell, running in Docker, pointed at a throwaway Immich.

> **Prompt:** Read CLAUDE.md, PLAN.md §5 and §8. Implement the hub per §5: registry loader (index.json + entry-point cross-check), catalog page, JSON-Schema→form config page, jobs page with htmx polling, webhook dispatcher with shared-secret check, `POST /api/run/{id}`, shared-password auth. Register all four addons in `registry/index.json` as stubs (entrypoints exist, `run()` raises NotImplemented) so the catalog renders four cards. Write the hub `Dockerfile` (python:3.12-slim; apt: ffmpeg, exiftool, WeasyPrint system deps), `deploy/docker-compose.yml` (hub on :8484, `/data` volume, `/dev/dri` device, joins the external immich network) and `deploy/dev/docker-compose.yml` (throwaway Immich server+ML+Postgres+Redis + hub). Add `fixtures/` (~30 CC0 photos incl. bursts and mixed orientations + 3 short CC0 clips) and `scripts/seed_dev.py` uploading them via the API. Finish: `docker compose -f deploy/dev/docker-compose.yml up` → seed → catalog shows 4 cards → probe.py PASS.

**Acceptance:** dev stack up; four cards render; a stub "Run" creates a job that fails cleanly and shows its log in the UI.
**Understand it:** the registry loader and the schema→form generator — explain how a JSON Schema becomes an HTML form and back into validated config.

### Phase 3 — auto-lut
> **Prompt:** Read CLAUDE.md, PLAN.md §6.1 and §4 (workflow wiring). Implement the auto-lut addon per spec, including the idempotency guards and EXIF copy. First implement `TRIGGER_MODE=poll`; then add the webhook path: log the raw payload from a real dev-instance workflow, commit an example payload into `tests/fixtures/`, and build the parser from it. Write `docs/workflow-setup.md` with the exact clicks for the Immich admin UI per §4. Ship 3 starter LUTs (CC0 .cube files) into `fixtures/luts/`.

**Acceptance:** §6.1 acceptance list, on the dev instance, both trigger modes.
**Understand it:** the ffmpeg `lut3d` invocation and the idempotency checks — explain why the graded copy can't re-trigger the addon.

### Phase 4 — trip-best-picks (+ shared scoring)
> **Prompt:** Read CLAUDE.md, PLAN.md §6.2. Implement `core/scoring.py` (sharpness, exposure, near_dup_groups, mmr_select) with unit tests on synthetic data, then the trip-best-picks addon per spec: both embedding sources, trip auto-detection, people boost, preview-grid dry-run UI, re-roll. Keep selection logic pure functions (arrays in, indices out) so it's testable without Immich.

**Acceptance:** §6.2 acceptance list on the dev fixtures.
**Understand it:** `mmr_select` — walk through one greedy iteration by hand; explain what λ trades off. (This function is the heart of three addons — worth genuinely understanding.)

### Phase 5 — zine-maker
> **Prompt:** Read CLAUDE.md, PLAN.md §6.3. Implement zine-maker per spec: candidate gathering (smart search or album), selection via core/scoring, sequencing (chronological + arc), aspect-aware template assignment, Jinja2+WeasyPrint rendering, `impose()` with golden tests for mini8 and booklet n=8/16, page previews in the hub UI, optional Ollama captions with exif fallback. Output both PDFs to /data/output.

**Acceptance:** §6.3 acceptance list; Harry prints and folds one mini8 before merge.
**Understand it:** `impose()` — explain why page 1 and page 16 share a printed side in the booklet; check the golden files against a hand-drawn folding diagram.

### Phase 6 — year-highlights
> **Prompt:** Read CLAUDE.md, PLAN.md §6.4. Implement year-highlights per spec: scene detection, scoring, quota-constrained selection, caching + resume, assembly with crossfades/ducking/title cards, QSV encode with fallback, album upload. Long-running job UX: granular progress (per-phase %, current clip) and a cancel button that leaves the cache valid.

**Acceptance:** §6.4 acceptance list, including the kill-and-resume test.
**Understand it:** the cache keying (asset ID + params hash) — explain what invalidates it and why resume is safe.

### Phase 7 — Hardening & quality of life
> **Prompt:** Read CLAUDE.md and PLAN.md §7 Phase 7. Add: (a) a scheduler (hub-side cron) so year-highlights can auto-run each January 1 and auto-lut can poll; (b) `docs/production.md`: moving from dev to the real instance (API key with minimal permissions, addons_ro role, allowed-hosts, dry-run-first checklist, backup note: /data and jobs.sqlite in the NAS backup set); (c) README with screenshots.

**Acceptance:** scheduled job fires in dev; docs reviewed; the README install path works on a clean machine.

### Phase 8 — In-app UI: the Immich fork
**Goal:** the store lives inside Immich's own web UI via a thin, rebase-friendly fork — until upstream offers a real extension point. Two Claude Code sessions, two repos.

**8a — hub side (this repo):**

> **Prompt:** Read CLAUDE.md and PLAN.md decision 6 + Phase 8. Prepare the hub for embedding: (1) `POST /api/inbox` accepts `{asset_ids: [...]}` from the Immich web origin, stores them as a short-lived selection token (SQLite, 15 min TTL), returns `{token}` — asset IDs must never travel in URLs or appear in logs; (2) catalog and addon config forms accept `#sel=<token>` and prefill their source field from it; (3) CORS: allow exactly `EMBED_ORIGIN` for `/api/inbox`, reject everything else; (4) framing: send `Content-Security-Policy: frame-ancestors <EMBED_ORIGIN>` (and never `X-Frame-Options: DENY`) so only Immich may iframe the hub; (5) a compact `?embedded=1` layout (no top nav) for iframe use. Tests: token TTL expiry, CORS rejection, sel-token prefill.

**8b — fork side (repo: fork of `immich-app/immich`):**

> **Prompt:** Read docs/FORK.md if it exists, else this phase spec. Create branch `addons/vX.Y.Z` from the latest upstream **release tag** (never main). Implement the "External apps" feature as exactly three isolated commits, fully generic and config-driven:
> 1. **config:** `IMMICH_EXTERNAL_APP_URL` (+ optional name/icon) exposed to the web client; extend the web CSP `frame-src` with that origin.
> 2. **panel:** sidebar entry + route `/external` rendering the configured URL in an iframe (append `?embedded=1`); hidden when unconfigured; admin toggle.
> 3. **action:** "Send to {app}" in the asset multi-select action bar and the album ⋯-menu → browser POSTs selected asset IDs to `{app}/api/inbox`, then navigates the panel route with `#sel=<token>`.
>
> Add `.github/workflows/fork-build.yml`: manual + weekly trigger → detect newer upstream tag → rebase the three commits onto it → build the server image → smoke test (container boots; `/external` serves 200 when configured) → push `ghcr.io/harrybma/immich:<tag>-addons`. Write `docs/FORK.md`: patch inventory (one paragraph per commit: what/why/files touched), the rebase ritual, the conflict playbook (if a rebase fights back: ship stock Immich — the hub is fully usable standalone), and a drafted upstream feature request for the "External apps" extension point, ready to post as a GitHub Discussion.

**Acceptance:** dev stack runs the forked image; a real multi-select → "Send to Addons" lands in a prefilled trip-best-picks form inside the iframe; swapping back to the stock image leaves every addon fully usable at :8484; the rebase workflow succeeds against at least one newer upstream tag.
**Understand it:** read all three fork commits end-to-end — they're deliberately small enough to fully grasp; explain the selection-token flow and why asset IDs never appear in URLs or server logs.

---

## 8. Test & dev environment

- **`deploy/dev/`** is a fully disposable Immich (its own volumes) + the hub. All development and all acceptance testing happens here against `fixtures/`. Production `.env` differs only in URLs/keys — switching is a config change, not a code change.
- Test pyramid: pure-function unit tests (scoring, imposition, selection — the majority), mocked-client tests (addon flows, idempotency), and a small `make demo` that runs each addon end-to-end against the dev stack.
- Golden files: imposition matrices, template assignment for known aspect-ratio sets, MMR picks for a synthetic embedding set (seeded RNG).

## 9. Backlog (explicitly not v1)

- Native **Wasm workflow steps** (Extism/@immich/plugin-sdk) for in-server filters — revisit when Workflows leaves preview.
- **Upstream the fork:** post the "External apps" design as an Immich feature request/Discussion; if accepted, PR the patch set and retire the fork.
- Tampermonkey userscript variant of the send-to-hub button — zero-maintenance fallback if fork rebasing ever becomes a chore.
- External addon containers + one-click install (docker API — a real security tradeoff to design deliberately).
- **3080 Ti remote worker** over Tailscale: GPU embeddings, NVENC encodes, bigger caption/aesthetic models.
- ONNX aesthetic model (NIMA-style) as an extra scoring term.
- Per-addon Immich API keys/scopes; signed registry entries for third-party addons.
- Hub UI rewrite in Svelte (matches the Immich stack and your learning path) once htmx feels limiting.

---

*Assumptions you can flip: hub port 8484, htmx-over-Svelte for v1, single shared hub password, MMR λ=0.7 default. Everything else is load-bearing.*
