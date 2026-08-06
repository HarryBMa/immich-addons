"""The additive-only rule and dry-run interception, as executable checks."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import httpx
import pytest

from immich_addons.core import client as client_mod
from immich_addons.core.client import ALLOWED_VERBS, ENDPOINTS, DryRunCall, ImmichClient

#: Method-name shapes that would mean the client can destroy or overwrite library data.
FORBIDDEN_NAME = re.compile(
    r"(^|_)(delete|destroy|remove|trash|purge|erase|overwrite|replace|rename)",
    re.IGNORECASE,
)


def _public_methods() -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(ImmichClient, callable)
        if not name.startswith("_") and getattr(member, "__module__", "") == client_mod.__name__
    ]


def test_no_destructive_methods_exist() -> None:
    """PLAN.md Phase 1 acceptance: the additive-only rule as executable code."""
    offenders = [name for name in _public_methods() if FORBIDDEN_NAME.search(name)]
    assert offenders == [], f"destructive-looking methods on ImmichClient: {offenders}"


def test_no_method_modifies_an_original() -> None:
    """`update_asset`-shaped names are banned too: originals are never edited in place."""
    offenders = [
        name
        for name in _public_methods()
        if name.startswith(("update_", "edit_", "set_", "patch_"))
    ]
    assert offenders == [], f"methods that would modify existing assets: {offenders}"


def test_every_endpoint_uses_an_allowed_verb() -> None:
    bad = {name: ep.verb for name, ep in ENDPOINTS.items() if ep.verb not in ALLOWED_VERBS}
    assert bad == {}, f"endpoints with a destructive verb: {bad}"


def test_request_refuses_a_destructive_verb(make_client, monkeypatch) -> None:  # noqa: ANN001
    """Even if someone adds a DELETE endpoint later, the request layer refuses to send it."""
    client = make_client()
    monkeypatch.setitem(
        ENDPOINTS, "danger", client_mod.Endpoint("DELETE", "/api/assets/{asset_id}", writes=True)
    )
    with pytest.raises(client_mod.ForbiddenMethodError):
        client._request("danger", path_params={"asset_id": "x"})
    assert client.recorded == []


def test_api_key_header_is_injected(make_client) -> None:  # noqa: ANN001
    client = make_client(json_body={"major": 3, "minor": 0, "patch": 1})
    client.server_version()
    assert client.recorded[0].headers["x-api-key"] == "test-key"


def test_server_version_is_assembled_from_parts(make_client) -> None:  # noqa: ANN001
    client = make_client(json_body={"major": 3, "minor": 0, "patch": 1})
    assert client.server_version() == "3.0.1"


def test_search_smart_unwraps_assets_items(make_client) -> None:  # noqa: ANN001
    body = {"assets": {"items": [{"id": "a"}, {"id": "b"}], "total": 2}}
    client = make_client(json_body=body)
    assert [a["id"] for a in client.search_smart("kids at the beach")] == ["a", "b"]


def test_dry_run_intercepts_writes_but_not_reads(make_client) -> None:  # noqa: ANN001
    client = make_client(json_body={"assets": {"items": []}}, dry_run=True)

    client.search_metadata(asset_type="IMAGE")  # a read: goes over the wire
    assert len(client.recorded) == 1

    result = client.create_album("Best of Gotland", asset_ids=["a", "b"])

    assert len(client.recorded) == 1, "a write escaped dry run"
    assert result == {"dryRun": True, "endpoint": "album_create"}
    assert len(client.dry_run_calls) == 1
    logged = client.dry_run_calls[0]
    assert isinstance(logged, DryRunCall)
    assert logged.verb == "POST"
    assert "Best of Gotland" in logged.summary
    assert logged.payload["json"] == {"albumName": "Best of Gotland", "assetIds": ["a", "b"]}


def test_dry_run_upload_does_not_read_the_file(make_client, tmp_path: Path) -> None:  # noqa: ANN001
    client = make_client(dry_run=True)
    src = tmp_path / "graded.jpg"
    src.write_bytes(b"not really a jpeg")

    client.upload_asset(src, device_asset_id="auto-lut_abc_1234")

    assert client.recorded == []
    assert "auto-lut_abc_1234" in client.dry_run_calls[0].summary


def test_writes_are_sent_when_not_in_dry_run(make_client) -> None:  # noqa: ANN001
    client = make_client(json_body={"id": "album-1"})
    client.create_album("Highlights 2026")
    request = client.recorded[0]
    assert request.method == "POST"
    assert request.url.path == "/api/albums"
    assert client.dry_run_calls == []


def test_download_original_streams_to_disk(settings, tmp_path: Path) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/assets/asset-1/original"
        return httpx.Response(200, content=b"\xff\xd8original bytes")

    client = ImmichClient(settings, dry_run=False, transport=httpx.MockTransport(handler))
    dest = client.download_original("asset-1", tmp_path / "sub" / "orig.jpg")
    assert dest.read_bytes() == b"\xff\xd8original bytes"


def test_http_errors_are_raised(make_client) -> None:  # noqa: ANN001
    client = make_client(lambda request: httpx.Response(404, json={"message": "nope"}))
    with pytest.raises(httpx.HTTPStatusError):
        client.asset_info("missing")


def test_iter_metadata_stops_on_a_short_page(settings) -> None:  # noqa: ANN001
    pages = {
        1: [{"id": f"a{i}"} for i in range(3)],
        2: [{"id": "a3"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        page = jsonlib.loads(request.content)["page"]
        return httpx.Response(200, json={"assets": {"items": pages.get(page, [])}})

    client = ImmichClient(settings, dry_run=False, transport=httpx.MockTransport(handler))
    assert [a["id"] for a in client.iter_metadata(size=3)] == ["a0", "a1", "a2", "a3"]
