"""End-to-end hub behaviour through FastAPI's TestClient. No Immich server involved."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from immich_addons.core.config import Settings
from immich_addons.core.jobs import JobStatus
from immich_addons.hub.app import RateLimiter, create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "registry" / "index.json"


@pytest.fixture
def hub_settings(tmp_path: Path) -> Settings:
    return Settings(
        immich_url="http://immich.test:2283",
        immich_api_key="test-key",
        hub_password="hunter2",
        hub_webhook_token="s3cret-token",
        data_dir=tmp_path / "data",
        _env_file=None,
    )


@pytest.fixture
def app(hub_settings: Settings):  # noqa: ANN201
    return create_app(hub_settings, registry_path=REGISTRY)


@pytest.fixture
def client(app) -> TestClient:  # noqa: ANN001
    with TestClient(app) as client:
        client.post("/login", data={"password": "hunter2"})
        yield client


@pytest.fixture
def anon(app) -> TestClient:  # noqa: ANN001
    with TestClient(app) as client:
        yield client


# --- auth ---------------------------------------------------------------------------------


def test_pages_require_a_login(anon: TestClient) -> None:
    response = anon.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_is_rejected(anon: TestClient) -> None:
    response = anon.post("/login", data={"password": "nope"})
    assert response.status_code == 401
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_login_then_logout(anon: TestClient) -> None:
    anon.post("/login", data={"password": "hunter2"})
    assert anon.get("/").status_code == 200
    anon.post("/logout")
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_healthz_needs_no_login(anon: TestClient) -> None:
    body = anon.get("/healthz").json()
    assert body == {"ok": True, "addons": 4, "installed": 4}


# --- catalog ------------------------------------------------------------------------------


def test_catalog_renders_a_card_per_addon(client: TestClient) -> None:
    """PLAN.md Phase 2 acceptance: four cards render."""
    html = client.get("/").text
    for name in ("Auto-LUT", "Trip Best Picks", "Zine Maker", "Year Highlights"):
        assert name in html
    assert html.count('class="card"') == 4


def test_enable_toggle_persists(client: TestClient, hub_settings: Settings) -> None:
    client.post("/addons/auto-lut/enabled", data={"enabled": "on"})
    from immich_addons.hub.registry import AddonStore

    assert AddonStore(hub_settings).is_enabled("auto-lut") is True


def test_unknown_addon_is_a_404(client: TestClient) -> None:
    assert client.get("/addons/nope").status_code == 404


# --- config form --------------------------------------------------------------------------


def test_config_form_is_generated_from_the_schema(client: TestClient) -> None:
    html = client.get("/addons/zine-maker").text
    assert 'name="layout"' in html
    assert 'value="booklet"' in html
    assert 'name="dry_run"' in html
    assert "checked" in html  # dry run defaults to on


def test_saving_valid_config_persists_it(client: TestClient, hub_settings: Settings) -> None:
    response = client.post(
        "/addons/auto-lut",
        data={"lut": "kodak.cube", "intensity": "0.6", "extensions": "jpg, heic", "dry_run": "on"},
    )
    assert response.status_code == 200
    assert "Saved." in response.text

    from immich_addons.hub.registry import AddonStore

    stored = AddonStore(hub_settings).config("auto-lut")
    assert stored["intensity"] == 0.6
    assert stored["extensions"] == ["jpg", "heic"]
    assert stored["dry_run"] is True


def test_saving_invalid_config_reports_and_does_not_persist(
    client: TestClient, hub_settings: Settings
) -> None:
    response = client.post("/addons/auto-lut", data={"intensity": "5", "dry_run": "on"})
    assert response.status_code == 422
    assert "intensity" in response.text

    from immich_addons.hub.registry import AddonStore

    assert AddonStore(hub_settings).config("auto-lut") == {}


# --- jobs ---------------------------------------------------------------------------------


def test_running_a_stub_addon_fails_cleanly_and_logs_why(client: TestClient, app) -> None:  # noqa: ANN001
    """PLAN.md Phase 2 acceptance: a stub Run creates a job that fails cleanly with its log."""
    response = client.post("/api/run/trip-best-picks", json={"dry_run": True})
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    jobs = app.state.jobs
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job and job.is_terminal:
            break
        time.sleep(0.02)

    job = jobs.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert "Phase 4" in job.log, "the log should say where the real implementation lands"
    assert "NotImplementedError" in job.log

    listing = client.get("/jobs").text
    assert "trip-best-picks" in listing
    assert client.get(f"/jobs/{job_id}").status_code == 200


def test_jobs_fragment_is_pollable(client: TestClient) -> None:
    response = client.get("/jobs/fragment")
    assert response.status_code == 200
    assert "<table" in response.text or "Nothing has run yet" in response.text


def test_cancelling_a_job(client: TestClient) -> None:
    job_id = client.post("/api/run/zine-maker", json={}).json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert client.post("/api/jobs/99999/cancel").status_code == 404


def test_running_an_uninstalled_addon_is_a_409(hub_settings: Settings, tmp_path: Path) -> None:
    registry = tmp_path / "index.json"
    registry.write_text(
        '{"registry_version": 1, "addons": [{"id": "ghost", "name": "Ghost", '
        '"entrypoint": "nope:Nope", "capabilities": ["manual"]}]}',
        encoding="utf-8",
    )
    with TestClient(create_app(hub_settings, registry_path=registry)) as client:
        client.post("/login", data={"password": "hunter2"})
        assert client.post("/api/run/ghost", json={}).status_code == 409


# --- webhook ------------------------------------------------------------------------------


def test_webhook_rejects_a_bad_token(anon: TestClient) -> None:
    response = anon.post("/hooks/immich", json={"asset": {"id": "a"}})
    assert response.status_code == 401
    assert anon.post("/hooks/immich/wrong", json={}).status_code == 401


def test_webhook_accepts_the_shared_secret_in_a_header(anon: TestClient) -> None:
    response = anon.post(
        "/hooks/immich", json={"asset": {"id": "a"}}, headers={"x-addons-token": "s3cret-token"}
    )
    assert response.status_code == 202


def test_webhook_accepts_the_secret_as_a_path_token(anon: TestClient) -> None:
    assert anon.post("/hooks/immich/s3cret-token", json={}).status_code == 202


def test_webhook_only_queues_enabled_webhook_addons(anon: TestClient, client: TestClient) -> None:
    body = anon.post("/hooks/immich/s3cret-token", json={}).json()
    assert body["queued"] == [], "nothing is enabled yet"

    client.post("/addons/auto-lut/enabled", data={"enabled": "on"})
    body = anon.post("/hooks/immich/s3cret-token", json={"asset": {"id": "a"}}).json()
    assert len(body["queued"]) == 1, "only auto-lut declares the webhook capability"


def test_webhook_is_refused_when_no_token_is_configured(tmp_path: Path) -> None:
    settings = Settings(
        immich_url="http://immich.test:2283",
        immich_api_key="k",
        hub_password="hunter2",
        hub_webhook_token="",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    with TestClient(create_app(settings, registry_path=REGISTRY)) as client:
        assert client.post("/hooks/immich/anything", json={}).status_code == 503


def test_the_token_never_appears_in_the_logs(anon: TestClient, caplog) -> None:  # noqa: ANN001
    with caplog.at_level("WARNING"):
        anon.post("/hooks/immich/s3cret-token", json={})
        anon.post("/hooks/immich/a-wrong-token", json={})
    assert "s3cret-token" not in caplog.text
    assert "a-wrong-token" not in caplog.text


def test_rate_limiter_windows() -> None:
    limiter = RateLimiter(limit=2, window_s=10)
    assert limiter.allow("1.2.3.4", now=0) is True
    assert limiter.allow("1.2.3.4", now=1) is True
    assert limiter.allow("1.2.3.4", now=2) is False
    assert limiter.allow("5.6.7.8", now=2) is True, "limits are per client"
    assert limiter.allow("1.2.3.4", now=20) is True, "the window moves on"


def test_hooks_are_rate_limited(anon: TestClient, app) -> None:  # noqa: ANN001
    app.state.limiter.limit = 3
    codes = [anon.post("/hooks/immich/s3cret-token", json={}).status_code for _ in range(5)]
    assert codes.count(429) >= 1
