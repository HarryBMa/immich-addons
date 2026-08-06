"""Read-only reader for Immich's CLIP embeddings.

This is the one place the project touches Immich's database directly, and it is **SELECT only**
(CLAUDE.md, "Library safety"). Three independent guards:

1. the ``addons_ro`` Postgres role has no write grants at all (set up by hand on the NAS);
2. :func:`connect` marks the session ``READ ONLY``, so the server refuses writes even if the role
   were over-granted;
3. :func:`_assert_read_only_sql` rejects any statement that is not a lone ``SELECT``/``WITH``.

Table and column names are **discovered, not hardcoded** — Immich has renamed these across
versions. :func:`find_embedding_column` looks for a ``vector``-typed column on a table that also
carries an asset id.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from immich_addons.core.config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

log = logging.getLogger(__name__)

_WRITE_WORD = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|merge)\b",
    re.IGNORECASE,
)

#: Column names that have carried the asset id on the embedding table, newest guess first.
ASSET_ID_CANDIDATES: tuple[str, ...] = ("assetId", "asset_id", "assetsId")

#: Preferred table-name fragments when several vector columns exist (e.g. face embeddings too).
TABLE_PREFERENCE: tuple[str, ...] = ("smart_search", "smartsearch", "smart", "clip")


class ReadOnlyViolationError(RuntimeError):
    """Raised when a statement that is not a plain read reaches the database layer."""


class EmbeddingSchemaError(RuntimeError):
    """Raised when no CLIP embedding table can be located by introspection."""


@dataclass(frozen=True)
class EmbeddingLocation:
    """Where the CLIP embeddings actually live on *this* server."""

    table: str
    column: str
    asset_id_column: str

    def describe(self) -> str:
        return f'"{self.table}"."{self.column}" keyed by "{self.asset_id_column}"'


def _assert_read_only_sql(sql: str) -> None:
    head = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
    if head not in {"select", "with"}:
        raise ReadOnlyViolationError(f"refusing non-SELECT statement: {sql[:60]!r}")
    if _WRITE_WORD.search(sql):
        raise ReadOnlyViolationError(f"refusing statement containing a write keyword: {sql[:60]!r}")


def connect(settings: Settings | None = None) -> Connection[Any]:
    """Open a read-only connection using the ``addons_ro`` credentials."""
    import psycopg  # imported lazily: the hub boots fine without a DB configured

    settings = settings or get_settings()
    conn = psycopg.connect(settings.db_dsn, autocommit=True)
    conn.read_only = True
    return conn


def query(conn: Connection[Any], sql: str, params: Any = None) -> list[tuple[Any, ...]]:
    """Run one read. Anything that is not a plain SELECT/WITH raises before it reaches Postgres."""
    _assert_read_only_sql(sql)
    with conn.cursor() as cur:
        cur.execute(sql, params)  # type: ignore[arg-type]
        return list(cur.fetchall())


VECTOR_COLUMNS_SQL = """
SELECT c.table_name, c.column_name
FROM information_schema.columns AS c
WHERE c.table_schema = 'public' AND c.udt_name = 'vector'
ORDER BY c.table_name, c.column_name
"""

TABLE_COLUMNS_SQL = """
SELECT c.table_name, c.column_name
FROM information_schema.columns AS c
WHERE c.table_schema = 'public' AND c.table_name = ANY(%s)
"""


def pick_embedding_location(
    vector_columns: list[tuple[str, str]],
    columns_by_table: dict[str, list[str]],
) -> EmbeddingLocation:
    """Choose the CLIP embedding table from introspection output.

    Pure function so it can be tested against snapshots of several Immich schemas without a
    database. Prefers a table whose name looks like smart search; otherwise takes the first
    vector-typed column on a table that also carries an asset id.
    """
    candidates: list[EmbeddingLocation] = []
    for table, column in vector_columns:
        cols = columns_by_table.get(table, [])
        lowered = {c.lower(): c for c in cols}
        for wanted in ASSET_ID_CANDIDATES:
            actual = lowered.get(wanted.lower())
            if actual:
                candidates.append(EmbeddingLocation(table, column, actual))
                break

    if not candidates:
        raise EmbeddingSchemaError(
            "no vector column found on a table carrying an asset id; "
            "run scripts/probe.py and check the schema by hand"
        )

    def rank(loc: EmbeddingLocation) -> tuple[int, str]:
        name = loc.table.lower()
        for i, fragment in enumerate(TABLE_PREFERENCE):
            if fragment in name:
                return (i, loc.table)
        return (len(TABLE_PREFERENCE), loc.table)

    return sorted(candidates, key=rank)[0]


def find_embedding_column(conn: Connection[Any]) -> EmbeddingLocation:
    """Locate the CLIP embedding table/column on the connected server."""
    vector_columns = [(str(t), str(c)) for t, c in query(conn, VECTOR_COLUMNS_SQL)]
    if not vector_columns:
        raise EmbeddingSchemaError(
            "no columns of type 'vector' in schema public — is the pgvector extension installed "
            "and has smart search run?"
        )
    tables = sorted({t for t, _ in vector_columns})
    columns_by_table: dict[str, list[str]] = {t: [] for t in tables}
    for table, column in query(conn, TABLE_COLUMNS_SQL, (tables,)):
        columns_by_table[str(table)].append(str(column))
    location = pick_embedding_location(vector_columns, columns_by_table)
    log.info("embeddings located at %s", location.describe())
    return location


def parse_vector(raw: Any) -> np.ndarray:
    """Turn pgvector's wire form into a float32 array.

    psycopg returns ``'[0.1,0.2,...]'`` unless the pgvector adapters are registered, and a list or
    array when they are. Handle all three so the caller never has to care.
    """
    if isinstance(raw, np.ndarray):
        return raw.astype(np.float32, copy=False)
    if isinstance(raw, (list, tuple)):
        return np.asarray(raw, dtype=np.float32)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    if isinstance(raw, str):
        text = raw.strip().strip("[]")
        if not text:
            return np.zeros(0, dtype=np.float32)
        return np.fromstring(text, sep=",", dtype=np.float32)
    raise TypeError(f"cannot parse vector from {type(raw).__name__}")


def embeddings_for(
    asset_ids: list[str],
    *,
    conn: Connection[Any] | None = None,
    location: EmbeddingLocation | None = None,
    settings: Settings | None = None,
) -> dict[str, np.ndarray]:
    """The only function addons need: asset id -> CLIP embedding.

    Assets without an embedding (not yet indexed by smart search) are simply absent from the
    result — callers decide whether to skip them or fall back to local CLIP.
    """
    if not asset_ids:
        return {}

    own_connection = conn is None
    conn = conn or connect(settings)
    try:
        location = location or find_embedding_column(conn)
        sql = (
            f'SELECT "{location.asset_id_column}", "{location.column}" '  # noqa: S608 - identifiers
            f'FROM "{location.table}" '
            f'WHERE "{location.asset_id_column}" = ANY(%s)'
        )
        rows = query(conn, sql, (list(asset_ids),))
        return {str(asset_id): parse_vector(vector) for asset_id, vector in rows}
    finally:
        if own_connection:
            conn.close()
