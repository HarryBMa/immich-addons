"""Introspection and the read-only guard, tested without a database."""

from __future__ import annotations

import numpy as np
import pytest

from immich_addons.core.db import (
    EmbeddingSchemaError,
    ReadOnlyViolationError,
    _assert_read_only_sql,
    parse_vector,
    pick_embedding_location,
)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM assets",
        "UPDATE assets SET x = 1",
        "INSERT INTO assets VALUES (1)",
        "DROP TABLE assets",
        "TRUNCATE assets",
        "SELECT 1; DELETE FROM assets",
        "  \n GRANT ALL ON assets TO addons_ro",
    ],
)
def test_write_statements_are_refused(sql: str) -> None:
    with pytest.raises(ReadOnlyViolationError):
        _assert_read_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        'select "assetId" from smart_search',
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_reads_are_allowed(sql: str) -> None:
    _assert_read_only_sql(sql)


def test_picks_the_smart_search_table_over_face_embeddings() -> None:
    location = pick_embedding_location(
        vector_columns=[("face_search", "embedding"), ("smart_search", "embedding")],
        columns_by_table={
            "face_search": ["faceId", "embedding", "assetId"],
            "smart_search": ["assetId", "embedding"],
        },
    )
    assert location.table == "smart_search"
    assert location.column == "embedding"
    assert location.asset_id_column == "assetId"


def test_accepts_a_snake_case_asset_id() -> None:
    """Immich has renamed columns across versions; introspection must not care about the style."""
    location = pick_embedding_location(
        vector_columns=[("smart_search", "embedding")],
        columns_by_table={"smart_search": ["asset_id", "embedding"]},
    )
    assert location.asset_id_column == "asset_id"


def test_falls_back_to_any_table_carrying_an_asset_id() -> None:
    location = pick_embedding_location(
        vector_columns=[("some_future_name", "embedding")],
        columns_by_table={"some_future_name": ["assetId", "embedding"]},
    )
    assert location.table == "some_future_name"


def test_raises_when_no_table_links_to_assets() -> None:
    with pytest.raises(EmbeddingSchemaError):
        pick_embedding_location(
            vector_columns=[("orphan", "embedding")],
            columns_by_table={"orphan": ["id", "embedding"]},
        )


@pytest.mark.parametrize(
    "raw",
    ["[0.5,-0.25,1.0]", b"[0.5,-0.25,1.0]", [0.5, -0.25, 1.0], (0.5, -0.25, 1.0)],
)
def test_parse_vector_handles_every_wire_form(raw: object) -> None:
    vector = parse_vector(raw)
    assert vector.dtype == np.float32
    np.testing.assert_allclose(vector, [0.5, -0.25, 1.0])


def test_parse_vector_on_an_empty_vector() -> None:
    assert parse_vector("[]").shape == (0,)


def test_parse_vector_rejects_nonsense() -> None:
    with pytest.raises(TypeError):
        parse_vector(object())
