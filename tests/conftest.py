"""Shared fixtures. No test in this suite may touch a real Immich server or a real database."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from immich_addons.core.client import ImmichClient
from immich_addons.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path) -> Settings:  # noqa: ANN001 - pytest's tmp_path
    return Settings(
        immich_url="http://immich.test:2283",
        immich_api_key="test-key",
        data_dir=tmp_path / "data",
        dry_run_default=False,
        _env_file=None,
    )


@pytest.fixture
def make_client(settings: Settings) -> Callable[..., ImmichClient]:
    """Build a client whose HTTP layer is a recording mock.

    The returned client carries ``.recorded``: every request the client actually sent.
    """

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
        *,
        dry_run: bool = False,
        json_body: Any = None,
    ) -> ImmichClient:
        recorded: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            if handler is not None:
                return handler(request)
            return httpx.Response(200, json=json_body if json_body is not None else {})

        client = ImmichClient(settings, dry_run=dry_run, transport=httpx.MockTransport(_handler))
        client.recorded = recorded  # type: ignore[attr-defined]
        return client

    return _factory
