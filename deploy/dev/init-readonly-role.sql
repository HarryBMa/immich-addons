-- Creates the read-only role the hub uses to read CLIP embeddings, exactly as PLAN.md §4
-- specifies it for the NAS — so dev and production differ in credentials, not in privileges.
--
-- Runs once, on an empty database, via the Postgres image's docker-entrypoint-initdb.d hook.
-- Immich creates its tables *after* this script, so the plain GRANT below covers nothing yet;
-- the ALTER DEFAULT PRIVILEGES line is what actually makes Immich's later tables readable.

CREATE ROLE addons_ro LOGIN PASSWORD 'devonly-readonly';

GRANT CONNECT ON DATABASE immich TO addons_ro;
GRANT USAGE ON SCHEMA public TO addons_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO addons_ro;

-- Tables Immich creates after this script runs would otherwise be invisible to addons_ro.
-- SELECT only: deliberately no INSERT/UPDATE/DELETE in the default privileges.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO addons_ro;
