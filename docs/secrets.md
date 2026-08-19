# Secrets

Everything secret is read from the **environment**: `IMMICH_API_KEY`, `IMMICH_DB_PASSWORD`,
`HUB_PASSWORD`, `HUB_WEBHOOK_TOKEN`. `pydantic-settings` reads a `.env` file when one is present,
but it never has to be — anything that puts those names in the process environment works, which is
what makes a vault a drop-in.

## With [vaulted](https://github.com/woosal1337/vaulted)

`vaulted run` injects decrypted secrets into a child process, so no plaintext file is ever written:

```sh
vaulted init --name immich-addons
vaulted set IMMICH_API_KEY            # hidden prompt
vaulted set HUB_PASSWORD
vaulted set HUB_WEBHOOK_TOKEN         # openssl rand -hex 32
vaulted set IMMICH_DB_PASSWORD

vaulted run -- uv run python scripts/probe.py
vaulted run -- uv run immich-addons-hub
vaulted run -- uv run pytest
```

Keep the dev and production values in separate vaulted environments and select them per command:

```sh
vaulted run -e dev  -- uv run python scripts/seed_dev.py
vaulted run -e prod -- uv run python scripts/probe.py
```

The dev/prod split matters more here than in most projects: pointing a dev run at the family
instance is the one mistake this repo is built to prevent. `seed_dev.py` refuses to upload to a URL
that does not look like a development instance, but that is a backstop, not the plan.

### Docker

Compose reads `env_file:`, which needs a real file on disk. Two options, in order of preference:

1. **Pass through from the vaulted process** — list the variable names under `environment:` with no
   value and run compose inside `vaulted run`, so the values come from the parent process:

   ```sh
   vaulted run -e prod -- docker compose -f deploy/docker-compose.yml up -d
   ```

2. **Export just in time**, and delete it afterwards:

   ```sh
   vaulted export -e prod -o .env && docker compose -f deploy/docker-compose.yml up -d && rm .env
   ```

`.env` and `.env.*` are gitignored (`.env.example` is the exception), so an exported file cannot be
committed by accident — but it is still plaintext on disk while it exists.

## Without a vault

Copy `.env.example` to `.env` and fill it in. That is all the code needs; the file is gitignored.

## What must never be a secret

`registry/index.json`, `contracts/*.json` and everything under `deploy/` are committed and public.
Do not put credentials in them. The dev stack's passwords in `deploy/dev/.env.example` are
deliberately worthless (`devonly`) and only ever reach a throwaway database.

## Rotating

The hub reads settings once at startup, so rotating a value means restarting the hub:

```sh
vaulted set IMMICH_API_KEY
docker compose -f deploy/docker-compose.yml restart addons-hub
```

Rotating `HUB_WEBHOOK_TOKEN` also means updating the webhook URL or header in Immich's workflow —
the hub will answer `401` until both sides match.
