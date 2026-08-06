"""Typed Immich REST client.

Two rules are enforced here rather than left to reviewer discipline (CLAUDE.md, "Library safety"):

1. **Additive only.** :meth:`ImmichClient._request` refuses any HTTP verb outside GET/POST/PUT, so
   no amount of later editing can make this client delete an asset or PATCH an original. There are
   deliberately no ``delete_*``/``update_asset``/``replace_*`` methods, and a unit test asserts the
   public surface stays that way.
2. **Dry run.** When ``dry_run`` is set, every write endpoint is logged to :attr:`dry_run_calls` and
   returns a synthetic response instead of touching the server. Reads still go over the wire, so a
   dry run exercises the whole pipeline.

Endpoint paths live in :data:`ENDPOINTS` and are **not** to be trusted from memory or documentation
— ``scripts/probe.py`` checks every one of them against the running server's OpenAPI spec. When
something 404s, fix it here and rerun the probe.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import httpx

from immich_addons.core.config import Settings, get_settings

log = logging.getLogger(__name__)

Verb = Literal["GET", "POST", "PUT"]
ALLOWED_VERBS: frozenset[str] = frozenset({"GET", "POST", "PUT"})


class ForbiddenMethodError(RuntimeError):
    """Raised if anything ever tries to send a destructive HTTP verb."""


@dataclass(frozen=True)
class Endpoint:
    verb: Verb
    path: str
    writes: bool = False


#: Every Immich endpoint this project uses. ``path`` may contain ``{}`` format fields.
ENDPOINTS: dict[str, Endpoint] = {
    # --- read ---------------------------------------------------------------------------
    "server_version": Endpoint("GET", "/api/server/version"),
    "server_about": Endpoint("GET", "/api/server/about"),
    "api_key_me": Endpoint("GET", "/api/api-keys/me"),
    "search_smart": Endpoint("POST", "/api/search/smart"),
    "search_metadata": Endpoint("POST", "/api/search/metadata"),
    "asset_info": Endpoint("GET", "/api/assets/{asset_id}"),
    "asset_original": Endpoint("GET", "/api/assets/{asset_id}/original"),
    "asset_thumbnail": Endpoint("GET", "/api/assets/{asset_id}/thumbnail"),
    "people": Endpoint("GET", "/api/people"),
    "albums": Endpoint("GET", "/api/albums"),
    "tags": Endpoint("GET", "/api/tags"),
    # --- write (additive only) ----------------------------------------------------------
    "asset_upload": Endpoint("POST", "/api/assets", writes=True),
    "album_create": Endpoint("POST", "/api/albums", writes=True),
    "album_add_assets": Endpoint("PUT", "/api/albums/{album_id}/assets", writes=True),
    "tag_create": Endpoint("POST", "/api/tags", writes=True),
    "tag_assign": Endpoint("PUT", "/api/tags/{tag_id}/assets", writes=True),
    "stack_create": Endpoint("POST", "/api/stacks", writes=True),
}

#: Search endpoints are POSTs that only read. Kept explicit so the probe can label them.
READ_ONLY_POSTS: frozenset[str] = frozenset({"search_smart", "search_metadata"})


@dataclass
class DryRunCall:
    """One write that *would* have happened."""

    endpoint: str
    verb: str
    url: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"DRY RUN {self.verb} {self.url} — {self.summary}"


class ImmichClient:
    """Thin, additive-only wrapper over the Immich REST API.

    Args:
        settings: resolved settings; defaults to the process-wide ones.
        dry_run: override ``DRY_RUN_DEFAULT``. When true, writes are logged, not sent.
        transport: injected for tests (``httpx.MockTransport``); no live calls in the test suite.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dry_run: bool | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.dry_run = self.settings.dry_run_default if dry_run is None else dry_run
        self.dry_run_calls: list[DryRunCall] = []
        self.settings.require("immich_url", "immich_api_key")
        self._http = httpx.Client(
            base_url=self.settings.immich_url,
            headers={
                "x-api-key": self.settings.immich_api_key,
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    # --- plumbing -----------------------------------------------------------------------

    def __enter__(self) -> ImmichClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        name: str,
        *,
        path_params: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        data: dict[str, Any] | None = None,
        dry_run_summary: str = "",
    ) -> httpx.Response | DryRunCall:
        ep = ENDPOINTS[name]
        if ep.verb not in ALLOWED_VERBS:
            # Unreachable via ENDPOINTS as written; this is the guard that keeps it that way.
            raise ForbiddenMethodError(
                f"{ep.verb} is not an allowed verb — this client is additive only"
            )
        url = ep.path.format(**(path_params or {}))

        if ep.writes and self.dry_run:
            call = DryRunCall(
                endpoint=name,
                verb=ep.verb,
                url=url,
                summary=dry_run_summary or name,
                payload={"json": json, "data": data, "params": params},
            )
            self.dry_run_calls.append(call)
            log.info("%s", call)
            return call

        response = self._http.request(
            ep.verb, url, json=json, params=params, files=files, data=data
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _json(result: httpx.Response | DryRunCall) -> Any:
        if isinstance(result, DryRunCall):
            return {"dryRun": True, "endpoint": result.endpoint}
        return result.json()

    # --- reads --------------------------------------------------------------------------

    def server_version(self) -> str:
        """``"3.0.1"`` style version string, assembled from the server's major/minor/patch."""
        body = self._json(self._request("server_version"))
        if isinstance(body, dict) and {"major", "minor", "patch"} <= body.keys():
            return f"{body['major']}.{body['minor']}.{body['patch']}"
        return str(body)

    def api_key_permissions(self) -> list[str]:
        """Permissions granted to the configured API key (used by the probe)."""
        body = self._json(self._request("api_key_me"))
        perms = body.get("permissions", []) if isinstance(body, dict) else []
        return [str(p) for p in perms]

    def openapi_spec(self) -> dict[str, Any]:
        """Fetch the live OpenAPI document. Path is not part of ENDPOINTS: it is not an API call
        we make during normal operation, only during probing."""
        response = self._http.get("/api/specs-v3")
        response.raise_for_status()
        return response.json()

    def search_smart(
        self,
        query: str,
        *,
        size: int = 80,
        page: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """CLIP text search. Returns the asset dicts from ``assets.items``."""
        payload: dict[str, Any] = {"query": query, "size": size, "page": page}
        payload.update(extra or {})
        body = self._json(self._request("search_smart", json=payload))
        return _assets_from_search(body)

    def search_metadata(
        self,
        *,
        taken_after: datetime | None = None,
        taken_before: datetime | None = None,
        asset_type: Literal["IMAGE", "VIDEO"] | None = None,
        person_ids: Sequence[str] | None = None,
        album_ids: Sequence[str] | None = None,
        size: int = 250,
        page: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Date/type/person filtered search. One page; see :meth:`iter_metadata`."""
        payload: dict[str, Any] = {"size": size, "page": page}
        if taken_after:
            payload["takenAfter"] = taken_after.isoformat()
        if taken_before:
            payload["takenBefore"] = taken_before.isoformat()
        if asset_type:
            payload["type"] = asset_type
        if person_ids:
            payload["personIds"] = list(person_ids)
        if album_ids:
            payload["albumIds"] = list(album_ids)
        payload.update(extra or {})
        body = self._json(self._request("search_metadata", json=payload))
        return _assets_from_search(body)

    def iter_metadata(self, *, max_pages: int = 200, **kwargs: Any) -> Iterable[dict[str, Any]]:
        """Page through :meth:`search_metadata` until a short page comes back."""
        size = int(kwargs.pop("size", 250))
        for page in range(1, max_pages + 1):
            batch = self.search_metadata(size=size, page=page, **kwargs)
            yield from batch
            if len(batch) < size:
                return

    def asset_info(self, asset_id: str) -> dict[str, Any]:
        return self._json(self._request("asset_info", path_params={"asset_id": asset_id}))

    def people(self) -> list[dict[str, Any]]:
        body = self._json(self._request("people"))
        if isinstance(body, dict):
            return list(body.get("people", []))
        return list(body)

    def albums(self) -> list[dict[str, Any]]:
        return list(self._json(self._request("albums")))

    def tags(self) -> list[dict[str, Any]]:
        return list(self._json(self._request("tags")))

    def download_original(self, asset_id: str, dest: Path) -> Path:
        """Stream the original file to ``dest``. Never modifies the asset on the server."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = ENDPOINTS["asset_original"].path.format(asset_id=asset_id)
        with self._http.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
        return dest

    def thumbnail(
        self, asset_id: str, *, size: Literal["preview", "thumbnail"] = "preview"
    ) -> bytes:
        result = self._request(
            "asset_thumbnail", path_params={"asset_id": asset_id}, params={"size": size}
        )
        assert isinstance(result, httpx.Response)  # reads are never intercepted
        return result.content

    # --- writes (additive only; intercepted by dry run) ----------------------------------

    def upload_asset(
        self,
        path: Path,
        *,
        device_asset_id: str,
        device_id: str = "immich-addons",
        file_created_at: datetime | None = None,
        file_modified_at: datetime | None = None,
        is_favorite: bool = False,
    ) -> dict[str, Any]:
        """Upload a **new** asset. Never overwrites an existing one.

        ``device_asset_id`` must be deterministic (``{addon}_{source-asset-id}_{hash}``) so a repeat
        run is recognised by the server as the same asset instead of creating a duplicate.
        """
        stat = path.stat()
        created = file_created_at or datetime.fromtimestamp(stat.st_mtime).astimezone()
        modified = file_modified_at or created
        form = {
            "deviceAssetId": device_asset_id,
            "deviceId": device_id,
            "fileCreatedAt": created.isoformat(),
            "fileModifiedAt": modified.isoformat(),
            "isFavorite": str(is_favorite).lower(),
        }
        if self.dry_run:
            return self._json(
                self._request(
                    "asset_upload",
                    data=form,
                    dry_run_summary=f"upload {path.name} as {device_asset_id}",
                )
            )
        with path.open("rb") as fh:
            return self._json(
                self._request(
                    "asset_upload",
                    data=form,
                    files={"assetData": (path.name, fh, "application/octet-stream")},
                )
            )

    def create_album(self, name: str, *, asset_ids: Sequence[str] = ()) -> dict[str, Any]:
        return self._json(
            self._request(
                "album_create",
                json={"albumName": name, "assetIds": list(asset_ids)},
                dry_run_summary=f"create album {name!r} with {len(asset_ids)} assets",
            )
        )

    def add_assets_to_album(self, album_id: str, asset_ids: Sequence[str]) -> dict[str, Any]:
        return self._json(
            self._request(
                "album_add_assets",
                path_params={"album_id": album_id},
                json={"ids": list(asset_ids)},
                dry_run_summary=f"add {len(asset_ids)} assets to album {album_id}",
            )
        )

    def create_tag(self, name: str) -> dict[str, Any]:
        return self._json(
            self._request("tag_create", json={"name": name}, dry_run_summary=f"create tag {name!r}")
        )

    def assign_tag(self, tag_id: str, asset_ids: Sequence[str]) -> dict[str, Any]:
        return self._json(
            self._request(
                "tag_assign",
                path_params={"tag_id": tag_id},
                json={"ids": list(asset_ids)},
                dry_run_summary=f"tag {len(asset_ids)} assets with {tag_id}",
            )
        )

    def create_stack(self, asset_ids: Sequence[str]) -> dict[str, Any]:
        """Stack assets together. The first id becomes the stack primary."""
        return self._json(
            self._request(
                "stack_create",
                json={"assetIds": list(asset_ids)},
                dry_run_summary=f"stack {len(asset_ids)} assets",
            )
        )


def _assets_from_search(body: Any) -> list[dict[str, Any]]:
    """Both search endpoints answer ``{"assets": {"items": [...]}}``; be forgiving anyway."""
    if isinstance(body, dict):
        assets = body.get("assets")
        if isinstance(assets, dict):
            return list(assets.get("items", []))
        if isinstance(assets, list):
            return list(assets)
        if isinstance(body.get("items"), list):
            return list(body["items"])
    if isinstance(body, list):
        return list(body)
    return []
