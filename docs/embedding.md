# Embedding the hub in Immich

The hub can live inside Immich's own web UI: a sidebar entry, an iframe, and a "Send to Addons"
action on a photo selection. This document is the **hub side of that contract** — everything a
client needs in order to hand a selection over. The Immich side is a small fork, and lives in its
own repository (PLAN.md Phase 8b).

Nothing here is required. With `EMBED_ORIGIN` unset the hub refuses to be framed at all and every
addon stays fully usable at `:8484`.

## The problem this solves

Selecting photos happens in Immich. Running an addon on them happens in the hub. The asset IDs have
to cross between two origins, and the obvious way — putting them in the URL — is the one thing that
is not acceptable: URLs end up in access logs, proxy logs, browser history and `Referer` headers,
and these IDs point at pictures of somebody's children.

So the IDs are POSTed once and replaced by an opaque token:

```
Immich page                     Hub
    |                            |
    |  POST /api/inbox           |     { "asset_ids": ["…", "…"] }
    |--------------------------->|
    |                            |     stores them, 15 min TTL
    |  201 { token, count }      |
    |<---------------------------|
    |                            |
    |  navigate to               |
    |  /addons/trip-best-picks#sel=<token>
    |                            |     the fragment is never sent to any server
    |                            |
    |  GET /api/selection/<token>|     from the hub's own page, with its session
    |--------------------------->|
    |  200 { asset_ids, count }  |
    |<---------------------------|
```

The result: no asset ID appears in a URL, a log line, or a `Referer` header at any point.

## Configuration

```sh
EMBED_ORIGIN=http://immich.example.lan:2283   # exactly the origin Immich's web UI is served from
```

Scheme, host and port, no trailing slash and no path. This one value drives three things: which
origin may call `/api/inbox`, which origin CORS permits, and which origin may frame the hub.

## `POST /api/inbox`

Accepts the selection. **Not session-authenticated** — the call is cross-origin from Immich's page,
where the hub's `SameSite=lax` cookie is not sent, so requiring a session would mean it could never
work. What guards it instead:

- the `Origin` header must equal `EMBED_ORIGIN` (checked in the handler, not only by CORS, so a
  non-browser client gets the same answer);
- CORS allows exactly that origin, `POST`/`OPTIONS`, and `content-type`;
- the shared rate limiter applies, per client address;
- the body is capped at 5000 IDs;
- the response contains a token and a count and **never the IDs**.

Writing a selection reveals nothing and grants nothing. Reading one is the privileged half.

```http
POST /api/inbox
Origin: http://immich.example.lan:2283
Content-Type: application/json

{"asset_ids": ["8f3c…", "9a1b…"]}
```

```json
{"token": "kQ7…", "count": 2, "ttl_s": 900}
```

| Status | Meaning |
| --- | --- |
| 201 | stored; use the token |
| 403 | the `Origin` is not `EMBED_ORIGIN` |
| 422 | `asset_ids` missing, not a list, empty, or over the cap |
| 429 | rate limited |
| 503 | `EMBED_ORIGIN` is not configured — the feature is off |

## `GET /api/selection/{token}`

Exchanges a token for its IDs. **Requires a hub session**, and is called by the hub's own page, so
it is same-origin. Returns 404 once the token has expired — 15 minutes — or if it never existed.
The token is in the path rather than a query string, and it is not an asset ID.

## Where the token goes

In the URL **fragment**: `#sel=<token>`. Browsers do not transmit fragments, so the token never
reaches the hub's access log either. `static/app.js` reads it on load and:

- on an addon page, exchanges it, fills the hidden `asset_ids` field, sets `source` to `selection`
  where the addon offers one, and clears the fragment from the address bar;
- on the catalog, shows a banner with the count and appends the fragment to every addon link, so
  the selection follows you to whichever addon you pick.

`trip-best-picks` and `zine-maker` both accept a selection. An addon opts in simply by declaring a
list field with `x-widget: selection` — the form renders it as a hidden field rather than a text
box, because nobody types asset IDs.

## Framing

Every response carries `Content-Security-Policy: frame-ancestors <EMBED_ORIGIN>`, or
`frame-ancestors 'none'` when unconfigured. The hub deliberately does **not** send
`X-Frame-Options: DENY`: the old header cannot express "this one origin", and would break the
embed entirely.

Add `?embedded=1` to any hub URL for the compact layout — no top bar, since Immich already has one.

## What the Immich fork must do

Three things, and they are all generic — nothing about this hub is hardcoded into Immich:

1. **Config:** an `IMMICH_EXTERNAL_APP_URL` (plus optional name and icon) exposed to the web
   client, with that origin added to the web CSP's `frame-src`.
2. **Panel:** a sidebar entry and a route that iframes the configured URL with `?embedded=1`,
   hidden when unconfigured.
3. **Action:** "Send to {app}" in the multi-select action bar and the album menu, which POSTs the
   selected IDs to `{app}/api/inbox` and then navigates to the panel route with `#sel=<token>`.

Kept to three isolated commits on an upstream release tag so they can be rebased forward; if a
rebase ever fights back, ship stock Immich and use the hub directly at `:8484`. Nothing is lost but
the convenience.
