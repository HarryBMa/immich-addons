# Going to production

Moving from the throwaway dev stack to the Immich instance that holds photos you cannot replace.
The order below is deliberate: every step narrows what the hub *can* do before anything is pointed
at the real library.

Do not skip to the end. The hub is additive by design, but "by design" is an argument, and an
argument is not a backup.

## 1. An API key with the narrowest permissions

Immich → **Account Settings → API Keys → New API Key**. Grant only what the addons you intend to
run actually need.

The permission *names* have changed across Immich versions, so rather than a list to copy blindly,
here is the complete set of calls the hub can make — every one of them, from
[`core/client.py`](../src/immich_addons/core/client.py) — and you tick the permissions in the UI
that cover them:

| Call | Needed by |
| --- | --- |
| `GET /api/server/version`, `/api/server/about`, `/api/api-keys/me` | all (the probe and the health check) |
| `GET /api/assets/{id}`, `/api/assets/{id}/original`, `/api/assets/{id}/thumbnail` | all |
| `POST /api/search/smart`, `/api/search/metadata` | zine-maker, trip-best-picks |
| `GET /api/people` | trip-best-picks (the face bonus) |
| `GET /api/albums`, `/api/albums/{id}`, `GET /api/tags` | all |
| `POST /api/assets` | anything that uploads |
| `POST /api/albums`, `PUT /api/albums/{id}/assets` | trip-best-picks |
| `POST /api/tags`, `PUT /api/tags/{id}/assets` | anything that uploads |
| `POST /api/stacks` | auto-lut |

That table is exhaustive: `ENDPOINTS` in `core/client.py` is the only place a request can be built,
and a test asserts nothing outside it is reachable. `scripts/probe.py` reports what the key you
created actually grants — run it and compare, rather than trusting this file after an upgrade.

**Grant no delete permission to this key.** The hub has no code path that calls a delete
endpoint — `core/client.py` refuses any verb outside `GET`/`POST`/`PUT` and a test asserts it — but
a key that *cannot* delete is a guarantee rather than a promise, and it survives future changes to
this repo that a code review might not.

Give the key a name that says what it is (`addons-hub`) so it is obvious what you are revoking if
you ever want it gone. Revoking it is the kill switch for everything below.

## 2. The read-only database role

Only `trip-best-picks` in `db` mode and `zine-maker` need Postgres, and only to read CLIP
embeddings. Create the role on the Immich database:

```sql
CREATE ROLE addons_ro LOGIN PASSWORD 'put-a-real-password-here';
GRANT CONNECT ON DATABASE immich TO addons_ro;
GRANT USAGE ON SCHEMA public TO addons_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO addons_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO addons_ro;
```

That is [`deploy/dev/init-readonly-role.sql`](../deploy/dev/init-readonly-role.sql) verbatim except
for the password, so dev and production differ in credentials and not in privileges. The final
line matters after an Immich upgrade: tables created later would otherwise be invisible.

`core/db.py` also enforces read-only-ness from its side — three separate guards — but as with the
API key, the grant is the guarantee and the code is the courtesy. Verify it:

```sh
psql "host=… dbname=immich user=addons_ro" -c "CREATE TABLE nope (i int);"
# ERROR:  permission denied for schema public
```

`local` embedding mode needs no database at all. If you would rather not create the role, set
`embedding_source=local` and leave `IMMICH_DB_*` empty; it is slower and otherwise equivalent.

## 3. Never hardcode a schema — probe it

Endpoint names and the embedding table have both moved between Immich versions. Run this against
the real server before the first addon run and after **every** Immich upgrade:

```sh
uv run python scripts/probe.py
```

Exit code 0 means the version, the endpoints the addons use, the API key's permissions and the
database role all check out. Commit the OpenAPI snapshot it writes under `contracts/` so the next
upgrade's drift is visible as a diff. Record the version in `.env` as `IMMICH_VERSION`.

## 4. Exposure

The hub has one shared password and is meant for a LAN or a Tailscale network. It is not hardened
for the open internet and should not be there.

- `HUB_PASSWORD` — set it. Empty means no login at all, and the catalog page says so in a banner.
- `HUB_BIND` — defaults to `127.0.0.1`. Widen it only to a specific interface address, never
  `0.0.0.0` on a machine with a public IP.
- `HUB_WEBHOOK_TOKEN` — `openssl rand -hex 32`. The hook compares it in constant time and never
  logs it. Prefer the `x-addons-token` header; the `/hooks/immich/{token}` path form exists only
  for Immich builds whose webhook step cannot send headers.
- Behind a reverse proxy (Traefik, Caddy, nginx), keep it on an internal router. If TLS terminates
  at the proxy, the hub's `https_only=False` session cookie is fine; if you ever expose it beyond
  a trusted network, that assumption stops holding.
- `EMBED_ORIGIN` — the origin of your Immich web UI, and only if you want the hub embedded in it.
  It decides who may call `/api/inbox`, who CORS permits, and who may frame the hub; empty means
  nobody, on all three counts. See [embedding.md](embedding.md).

## 5. Dry-run first — the actual checklist

Every addon ships with `dry_run: true`. A dry run performs the whole pipeline — downloads,
decoding, scoring, layout, encoding — and logs the API writes it *would* have made instead of
making them. It is a rehearsal, not a simulation.

For each addon, in order:

1. **Enable it, configure it, leave `dry_run` on.** Run it from the catalog.
2. **Read the job log.** Every intended write is there: which asset, which album, which tag. If a
   line surprises you, stop.
3. **Check the artifacts.** PDFs and films land in `/data/output` and are produced for real even in
   dry run — that is the point. Open them.
4. **Turn `dry_run` off and run it once, narrowly** — one album, one short date range, one LUT.
5. **Look at the result in Immich.** The new assets carry an `addon:<name>` tag. Search that tag:
   it is the complete list of everything the hub has ever added, and deleting that selection
   undoes the run.
6. **Then widen the scope.**

Between steps 4 and 6, on the first real run of `auto-lut`, confirm the loop guard: the graded copy
must not itself trigger a second grading. It carries an `addon:auto-lut` tag and a filename matching
`auto-lut_<asset>_<hash>.jpg`, and either one is enough for the addon to skip it. The job log for
the second event should say so explicitly.

## 6. Schedules

Schedules live per addon on its config page (`year-highlights` and `auto-lut`'s polling mode).
Three things worth knowing before you rely on one:

- **Times are read in `HUB_TIMEZONE`.** Leave it empty and the container's clock decides, which is
  usually UTC — so "1 January at 03:00" quietly becomes 04:00 in Stockholm. Set it.
- **A schedule only fires while the addon is enabled.** The toggle is the master switch.
- **Missed runs are not repaid.** If the hub was down when a schedule was due it fires once on the
  next tick, and if the miss is more than six hours old it is logged and skipped rather than run
  late. A year film for last January is not wanted in March.

## 7. Backups

Two things the hub owns are not in Immich and will not come back on their own:

| Path | What it is |
| --- | --- |
| `/data` (the `hub-data` volume) | LUTs, music, addon config, the analysis cache, and every PDF and film produced under `output/` |
| `/data/jobs.sqlite` | the job history: what ran, when, with what config, and what it created |

**Add the `hub-data` volume to the NAS backup set.** It is small — the cache and outputs dominate,
and both are regenerable — but the addon config and the job history are not regenerable, and the
job history is the audit trail for everything the hub has ever written to the library.

`/data/.session_key` is in there too; losing it logs everyone out and nothing more.

Immich's own library and database are backed up by whatever already backs up Immich. Nothing here
changes that, and nothing here is a substitute for it.

## 8. If something goes wrong

Everything the hub adds is tagged. In Immich, search the tag:

- `addon:auto-lut` — graded copies
- `addon:trip-best-picks` — album members
- `addon:zine-maker`, `addon:year-highlights` — anything uploaded

Selecting a tag and deleting the selection undoes that addon's work, and only that addon's work.
Originals are never modified, so there is nothing to restore — only additions to remove.

To stop everything at once: revoke the API key from step 1. The hub keeps running and every job
fails loudly at the first request, which is the intended failure mode.
