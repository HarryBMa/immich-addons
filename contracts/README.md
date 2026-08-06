# contracts/

Snapshots of the Immich server's own OpenAPI document, one file per version:
`openapi-<version>.json`, written by `scripts/probe.py`.

They exist so API drift is visible as a **diff** rather than as a 404 in production. After an
Immich upgrade:

```sh
uv run python scripts/probe.py
git diff contracts/
```

A renamed path or a changed request body shows up right there, and `probe.py` fails the specific
endpoint check that broke. Fix `ENDPOINTS` in `src/immich_addons/core/client.py`, rerun, commit the
new snapshot alongside the fix.

Never hand-edit these files, and never treat one as the truth for a *different* server version —
the running server is always the source of truth (CLAUDE.md, "Schema truth").
