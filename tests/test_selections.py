"""The selection handover: token store, the inbox endpoint, CORS and framing (PLAN.md Phase 8a).

The property under test throughout is the one the plan is emphatic about: asset IDs must never
travel in a URL or appear in a log.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from immich_addons.core.config import Settings
from immich_addons.hub.app import create_app
from immich_addons.hub.selections import MAX_ASSET_IDS, SelectionError, SelectionStore

REGISTRY = Path(__file__).resolve().parents[1] / "registry" / "index.json"
ORIGIN = "http://immich.test:2283"


# --- the store ------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SelectionStore:
    return SelectionStore(tmp_path / "sel.sqlite")


def test_a_selection_round_trips(store: SelectionStore) -> None:
    selection = store.create(["a", "b", "c"])
    assert selection.count == 3

    resolved = store.resolve(selection.token)
    assert resolved is not None
    assert resolved.asset_ids == ("a", "b", "c")


def test_tokens_are_unguessable_and_unique(store: SelectionStore) -> None:
    tokens = {store.create(["a"]).token for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 24 for t in tokens)


def test_a_token_expires(store: SelectionStore) -> None:
    """PLAN.md Phase 8a: 15 minute TTL."""
    selection = store.create(["a"], now=1000.0)
    assert store.resolve(selection.token, now=1000.0 + 14 * 60) is not None
    assert store.resolve(selection.token, now=1000.0 + 16 * 60) is None


def test_expired_rows_are_purged_not_just_hidden(store: SelectionStore) -> None:
    store.create(["a"], now=1000.0)
    store.resolve("anything", now=1000.0 + 3600)

    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM selections").fetchone()[0] == 0


def test_consuming_a_token_stops_it_being_replayed(store: SelectionStore) -> None:
    token = store.create(["a"]).token
    assert store.consume(token) is not None
    assert store.consume(token) is None


def test_an_unknown_token_is_simply_absent(store: SelectionStore) -> None:
    assert store.resolve("not-a-real-token") is None


def test_empty_and_oversized_selections_are_refused(store: SelectionStore) -> None:
    with pytest.raises(SelectionError, match="no asset ids"):
        store.create([])
    with pytest.raises(SelectionError, match="no asset ids"):
        store.create(["", "   "])
    with pytest.raises(SelectionError, match="too many"):
        store.create([f"a{i}" for i in range(MAX_ASSET_IDS + 1)])


def test_the_store_never_logs_asset_ids(store: SelectionStore, caplog) -> None:  # noqa: ANN001
    with caplog.at_level(logging.DEBUG):
        store.create(["secret-asset-id-1", "secret-asset-id-2"])
    assert "secret-asset-id" not in caplog.text
    assert "2 asset(s)" in caplog.text


def test_a_selection_survives_a_restart(tmp_path: Path) -> None:
    """On disk, not in memory: the hub may restart between 'Send to Addons' and the form loading."""
    path = tmp_path / "sel.sqlite"
    token = SelectionStore(path).create(["a", "b"]).token
    assert SelectionStore(path).resolve(token) is not None


# --- the endpoints --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        immich_url=ORIGIN,
        immich_api_key="k",
        hub_password="hunter2",
        embed_origin=ORIGIN,
        data_dir=tmp_path / "data",
        _env_file=None,
    )


@pytest.fixture
def app(settings: Settings):  # noqa: ANN201
    return create_app(settings, registry_path=REGISTRY)


@pytest.fixture
def client(app) -> TestClient:  # noqa: ANN001
    with TestClient(app) as client:
        client.post("/login", data={"password": "hunter2"})
        yield client


@pytest.fixture
def anon(app) -> TestClient:  # noqa: ANN001
    with TestClient(app) as client:
        yield client


def test_the_inbox_takes_a_selection_and_returns_only_a_token(anon: TestClient) -> None:
    response = anon.post("/api/inbox", json={"asset_ids": ["a1", "a2"]}, headers={"origin": ORIGIN})
    assert response.status_code == 201

    body = response.json()
    assert body["count"] == 2
    assert body["token"]
    assert "asset_ids" not in body, "the response must not echo the IDs back"


def test_the_inbox_does_not_need_a_hub_session(anon: TestClient) -> None:
    """It is called cross-origin from Immich's page, where the SameSite=lax cookie is not sent.
    Requiring a session would mean it could never work."""
    response = anon.post("/api/inbox", json={"asset_ids": ["a"]}, headers={"origin": ORIGIN})
    assert response.status_code == 201


def test_reading_a_selection_does_need_one(anon: TestClient, client: TestClient) -> None:
    token = anon.post("/api/inbox", json={"asset_ids": ["a1"]}, headers={"origin": ORIGIN}).json()[
        "token"
    ]

    assert anon.get(f"/api/selection/{token}", follow_redirects=False).status_code == 303
    assert client.get(f"/api/selection/{token}").json()["asset_ids"] == ["a1"]


def test_a_foreign_origin_is_rejected(anon: TestClient) -> None:
    response = anon.post(
        "/api/inbox", json={"asset_ids": ["a"]}, headers={"origin": "http://evil.test"}
    )
    assert response.status_code == 403


def test_cors_allows_exactly_the_embed_origin(anon: TestClient) -> None:
    allowed = anon.options(
        "/api/inbox",
        headers={
            "origin": ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert allowed.headers.get("access-control-allow-origin") == ORIGIN

    denied = anon.options(
        "/api/inbox",
        headers={"origin": "http://evil.test", "access-control-request-method": "POST"},
    )
    assert denied.headers.get("access-control-allow-origin") is None


def test_the_inbox_is_off_when_no_embed_origin_is_configured(tmp_path: Path) -> None:
    settings = Settings(
        immich_url=ORIGIN,
        hub_password="x",
        embed_origin="",
        data_dir=tmp_path / "d",
        _env_file=None,
    )
    with TestClient(create_app(settings, registry_path=REGISTRY)) as client:
        assert client.post("/api/inbox", json={"asset_ids": ["a"]}).status_code == 503


def test_a_junk_payload_is_a_422(anon: TestClient) -> None:
    headers = {"origin": ORIGIN}
    assert anon.post("/api/inbox", json={"asset_ids": "a1"}, headers=headers).status_code == 422
    assert anon.post("/api/inbox", json={}, headers=headers).status_code == 422


def test_the_inbox_is_rate_limited(anon: TestClient, app) -> None:  # noqa: ANN001
    app.state.limiter.limit = 3
    codes = [
        anon.post("/api/inbox", json={"asset_ids": ["a"]}, headers={"origin": ORIGIN}).status_code
        for _ in range(6)
    ]
    assert 429 in codes


def test_an_expired_token_says_so(client: TestClient, app) -> None:  # noqa: ANN001
    selection = app.state.selections.create(["a"], now=0.0)
    response = client.get(f"/api/selection/{selection.token}")
    assert response.status_code == 404
    assert "expired" in response.json()["detail"]


def test_asset_ids_never_reach_the_logs(anon: TestClient, client: TestClient, caplog) -> None:  # noqa: ANN001
    with caplog.at_level(logging.DEBUG):
        token = anon.post(
            "/api/inbox",
            json={"asset_ids": ["never-log-me"]},
            headers={"origin": ORIGIN},
        ).json()["token"]
        client.get(f"/api/selection/{token}")
    assert "never-log-me" not in caplog.text


# --- framing --------------------------------------------------------------------------------


def test_only_immich_may_frame_the_hub(anon: TestClient) -> None:
    response = anon.get("/login")
    assert response.headers["content-security-policy"] == f"frame-ancestors {ORIGIN}"
    assert "x-frame-options" not in response.headers, "DENY would break the embed entirely"


def test_with_no_embed_origin_nobody_may_frame_it(tmp_path: Path) -> None:
    settings = Settings(
        immich_url=ORIGIN,
        hub_password="x",
        embed_origin="",
        data_dir=tmp_path / "d",
        _env_file=None,
    )
    with TestClient(create_app(settings, registry_path=REGISTRY)) as client:
        assert client.get("/login").headers["content-security-policy"] == "frame-ancestors 'none'"


# --- the embedded layout and the prefill ----------------------------------------------------


def test_the_embedded_layout_drops_the_chrome(client: TestClient) -> None:
    plain = client.get("/").text
    embedded = client.get("/?embedded=1").text

    assert "<header" in plain
    assert "<header" not in embedded, "Immich already has a nav; two would be silly"
    assert 'class="embedded"' in embedded


def test_the_selection_field_is_hidden_not_a_text_box(client: TestClient) -> None:
    """Nobody types asset IDs. The field exists only for app.js to fill from the token."""
    html = client.get("/addons/trip-best-picks").text
    assert 'name="asset_ids"' in html
    assert 'type="hidden"' in html
    assert "data-selection-input" in html


def test_the_selection_source_is_offered(client: TestClient) -> None:
    assert 'value="selection"' in client.get("/addons/trip-best-picks").text


def test_a_posted_selection_is_stored_as_config(client: TestClient, app) -> None:  # noqa: ANN001
    """What the prefilled form submits has to survive validation and reach the addon."""
    response = client.post(
        "/addons/trip-best-picks",
        data={"source": "selection", "asset_ids": "a1,a2,a3", "n_picks": "5", "dry_run": "on"},
    )
    assert response.status_code == 200, response.text

    stored = app.state.store.config("trip-best-picks")
    assert stored["asset_ids"] == ["a1", "a2", "a3"]
    assert stored["source"] == "selection"


def test_a_selection_source_without_a_selection_is_refused(client: TestClient) -> None:
    response = client.post(
        "/addons/trip-best-picks", data={"source": "selection", "asset_ids": "", "dry_run": "on"}
    )
    assert response.status_code == 422
    assert "asset_ids" in response.text
