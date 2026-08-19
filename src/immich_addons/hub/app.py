"""The hub: catalog, config forms, job list, webhook dispatcher (PLAN.md §5).

Security posture, in one place so it is reviewable:

* one shared password, held in a signed session cookie; every page except ``/login``, ``/healthz``
  and ``/hooks/*`` requires it;
* ``/hooks/*`` authenticates with a shared secret compared in constant time, is rate limited per
  client address, and never logs the token;
* the hub binds to the LAN/Tailscale only and is never exposed to the internet.

The webhook handler does no work: it validates, enqueues, and returns 200 immediately, because
Immich's Workflows step should not be waiting on ffmpeg.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import deque
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from immich_addons.addons.base import Addon
from immich_addons.core.config import Settings, get_settings
from immich_addons.core.jobs import JobContext, JobQueue
from immich_addons.hub.forms import form_to_dict, schema_to_fields
from immich_addons.hub.registry import AddonStore, CatalogEntry, build_catalog
from immich_addons.hub.schedule import PRESETS, CronError, Scheduler, describe
from immich_addons.hub.selections import SelectionError, SelectionStore

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

#: Webhook rate limit: at most this many hook requests per window, per client address.
HOOK_RATE_LIMIT = 60
HOOK_RATE_WINDOW_S = 60.0


class RateLimiter:
    """Fixed-window counter per key. In-memory: one hub process, one worker."""

    def __init__(self, limit: int = HOOK_RATE_LIMIT, window_s: float = HOOK_RATE_WINDOW_S) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def _resumable_addon_ids(catalog: Iterable[CatalogEntry]) -> set[str]:
    return {
        entry.id
        for entry in catalog
        if entry.addon is not None and getattr(type(entry.addon), "resumable", False)
    }


def _runner_for(addon: Addon, store: AddonStore):  # noqa: ANN202 - returns a jobs.Runner
    """Wrap an addon so the queue hands it a validated config alongside the job context."""

    def run(ctx: JobContext) -> None:
        raw = {**store.config(addon.id), **ctx.params.get("config", {})}
        config = type(addon).parse_config(raw)
        if config.dry_run:
            ctx.log("dry run: intended writes will be logged, not sent to Immich")
        addon.run(ctx, config)

    return run


def create_app(settings: Settings | None = None, *, registry_path: Path | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_data_dirs()

    catalog = build_catalog(registry_path)
    store = AddonStore(settings)
    jobs = JobQueue(settings.jobs_db_path, resumable_addons=_resumable_addon_ids(catalog))
    for entry in catalog:
        if entry.addon is not None:
            jobs.register(entry.id, _runner_for(entry.addon, store))
    limiter = RateLimiter()
    scheduler = Scheduler(catalog, store, jobs, tz_name=settings.hub_timezone)
    selections = SelectionStore(settings.data_dir / "selections.sqlite")

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202 - fastapi lifespan
        jobs.sweep_interrupted()
        jobs.start()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            jobs.stop()

    app = FastAPI(title="Immich Addons", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.jobs = jobs
    app.state.store = store
    app.state.catalog = catalog
    app.state.limiter = limiter
    app.state.scheduler = scheduler
    app.state.selections = selections

    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(settings),
        same_site="lax",
        https_only=False,  # LAN/Tailscale only; Traefik terminates TLS in front when used
    )
    if settings.embed_origin:
        # Exactly one origin, and only the inbox. Immich's page is cross-origin to the hub, so the
        # session cookie is not sent with this request — see the note on the route itself.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.embed_origin],
            allow_methods=["POST", "OPTIONS"],
            allow_headers=["content-type"],
            max_age=600,
        )

    @app.middleware("http")
    async def _framing(request: Request, call_next):  # noqa: ANN001, ANN202
        """Say who may frame the hub.

        A CSP ``frame-ancestors`` rather than ``X-Frame-Options: DENY``: the older header cannot
        express "this one origin", and sending DENY would break the embed the whole phase is for.
        With no ``EMBED_ORIGIN`` configured the answer is nobody.
        """
        response = await call_next(request)
        allowed = settings.embed_origin or "'none'"
        response.headers["Content-Security-Policy"] = f"frame-ancestors {allowed}"
        return response

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["hub_title"] = "Addons"

    # --- auth ---------------------------------------------------------------------------

    def require_login(request: Request) -> None:
        if not settings.hub_password:
            return  # unconfigured: refuse to pretend there is a lock on the door
        if not request.session.get("authed"):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
                detail="login required",
            )

    @app.exception_handler(HTTPException)
    async def _redirect_on_login(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == status.HTTP_303_SEE_OTHER and "Location" in (exc.headers or {}):
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith(("/api/", "/hooks/")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request, "error.html", {"detail": exc.detail}, status_code=exc.status_code
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        return templates.TemplateResponse(request, "login.html", {"error": ""})

    @app.post("/login")
    async def login(request: Request, password: str = Form(default="")) -> Response:
        if settings.hub_password and hmac.compare_digest(password, settings.hub_password):
            request.session["authed"] = True
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Wrong password."}, status_code=401
        )

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "addons": len(catalog),
            "installed": sum(1 for e in catalog if e.installed),
        }

    # --- catalog ------------------------------------------------------------------------

    def _next_run_label(addon_id: str) -> str:
        """Only shown for addons the store has enabled — a schedule on a disabled addon does not
        fire, and a card promising a run that will not happen is worse than a blank."""
        if not store.is_enabled(addon_id):
            return ""
        when = scheduler.next_run(addon_id)
        return when.strftime("%Y-%m-%d %H:%M") if when else ""

    def _entry(addon_id: str) -> CatalogEntry:
        for entry in catalog:
            if entry.id == addon_id:
                return entry
        raise HTTPException(status_code=404, detail=f"no addon {addon_id!r} in the registry")

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def index(request: Request) -> Response:
        cards = [
            {
                "entry": entry,
                "enabled": store.is_enabled(entry.id),
                "configured": bool(store.config(entry.id)),
                "next_run": _next_run_label(entry.id),
            }
            for entry in catalog
        ]
        return templates.TemplateResponse(
            request,
            "catalog.html",
            {
                "cards": cards,
                "embedded": request.query_params.get("embedded") == "1",
                "unlocked": not settings.hub_password,
            },
        )

    @app.post("/addons/{addon_id}/enabled", dependencies=[Depends(require_login)])
    async def set_enabled(addon_id: str, enabled: str = Form(default="")) -> Response:
        entry = _entry(addon_id)
        if not entry.installed:
            raise HTTPException(status_code=409, detail="addon is not installed")
        store.set_enabled(addon_id, enabled == "on")
        return RedirectResponse("/", status_code=303)

    def _schedule_context(entry: CatalogEntry, *, error: str = "") -> dict[str, Any]:
        """Everything the schedule panel needs, or ``schedulable: False`` if the addon has no
        ``schedule``/``poll`` capability and the panel should not render at all."""
        if entry.id not in scheduler.scheduled_ids():
            return {"schedulable": False}
        state = scheduler.read(entry.id)
        next_run = scheduler.next_run(entry.id)
        return {
            "schedulable": True,
            "cron": state.cron,
            "cron_label": describe(state.cron) if state.cron else "",
            "cron_error": error,
            "presets": PRESETS,
            "next_run": next_run.strftime("%Y-%m-%d %H:%M") if next_run else "",
            "timezone": settings.hub_timezone or "server local time",
        }

    @app.get(
        "/addons/{addon_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_login)],
    )
    async def addon_page(request: Request, addon_id: str) -> Response:
        entry = _entry(addon_id)
        schema = entry.config_schema()
        saved = {**entry.default_config(), **store.config(addon_id)}
        return templates.TemplateResponse(
            request,
            "addon.html",
            {
                "entry": entry,
                "fields": schema_to_fields(schema, saved),
                "enabled": store.is_enabled(addon_id),
                "errors": [],
                "saved": False,
                "pickers": _picker_values(settings),
                "schedule": _schedule_context(entry),
                "embedded": request.query_params.get("embedded") == "1",
            },
        )

    @app.post(
        "/addons/{addon_id}/schedule",
        response_class=HTMLResponse,
        dependencies=[Depends(require_login)],
    )
    async def save_schedule(
        request: Request, addon_id: str, cron: str = Form(default="")
    ) -> Response:
        """Set or clear a schedule. An unparseable expression re-renders the page with the field
        named, rather than redirecting and losing what was typed."""
        entry = _entry(addon_id)
        if entry.id not in scheduler.scheduled_ids():
            raise HTTPException(status_code=409, detail="this addon cannot be scheduled")
        try:
            scheduler.save(addon_id, cron)
        except CronError as exc:
            return _render_addon(request, entry, schedule_error=str(exc), status_code=422)
        return RedirectResponse(f"/addons/{addon_id}", status_code=303)

    def _render_addon(
        request: Request,
        entry: CatalogEntry,
        *,
        schedule_error: str = "",
        status_code: int = 200,
    ) -> Response:
        saved = {**entry.default_config(), **store.config(entry.id)}
        return templates.TemplateResponse(
            request,
            "addon.html",
            {
                "entry": entry,
                "fields": schema_to_fields(entry.config_schema(), saved),
                "enabled": store.is_enabled(entry.id),
                "errors": [],
                "saved": False,
                "pickers": _picker_values(settings),
                "schedule": _schedule_context(entry, error=schedule_error),
                "embedded": request.query_params.get("embedded") == "1",
            },
            status_code=status_code,
        )

    @app.post(
        "/addons/{addon_id}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_login)],
    )
    async def save_addon(request: Request, addon_id: str) -> Response:
        entry = _entry(addon_id)
        schema = entry.config_schema()
        form = await request.form()
        submitted = form_to_dict(schema, _multi(form))

        errors: list[str] = []
        if entry.addon is not None:
            try:
                config = type(entry.addon).parse_config(submitted)
            except ValidationError as exc:
                errors = [_readable(e) for e in exc.errors()]
            else:
                store.save_config(addon_id, config.model_dump(mode="json"))
                submitted = config.model_dump(mode="json")

        return templates.TemplateResponse(
            request,
            "addon.html",
            {
                "entry": entry,
                "fields": schema_to_fields(schema, submitted),
                "enabled": store.is_enabled(addon_id),
                "errors": errors,
                "saved": not errors,
                "pickers": _picker_values(settings),
                "schedule": _schedule_context(entry),
                "embedded": request.query_params.get("embedded") == "1",
            },
            status_code=422 if errors else 200,
        )

    # --- jobs ---------------------------------------------------------------------------

    @app.get("/jobs", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def jobs_page(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "jobs.html",
            {
                "jobs": jobs.list(limit=50),
                "embedded": request.query_params.get("embedded") == "1",
            },
        )

    @app.get("/jobs/fragment", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def jobs_fragment(request: Request) -> Response:
        """Polled every 2 s by the jobs page."""
        return templates.TemplateResponse(
            request, "_jobs_table.html", {"jobs": jobs.list(limit=50)}
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def job_page(request: Request, job_id: int) -> Response:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        return templates.TemplateResponse(request, "job.html", {"job": job})

    @app.post("/api/run/{addon_id}", dependencies=[Depends(require_login)])
    async def run_addon(addon_id: str, request: Request) -> JSONResponse:
        entry = _entry(addon_id)
        if not entry.installed:
            raise HTTPException(status_code=409, detail="addon is not installed")
        params = await _json_or_form(request)
        job_id = jobs.enqueue(addon_id, {"trigger": "manual", "config": params})
        return JSONResponse({"job_id": job_id}, status_code=202)

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_login)])
    async def cancel_job(job_id: int) -> JSONResponse:
        if jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id}")
        jobs.cancel(job_id)
        return JSONResponse({"cancelled": job_id})

    # --- selection handover (Phase 8a) --------------------------------------------------

    @app.post("/api/inbox")
    async def inbox(request: Request) -> JSONResponse:
        """Accept a selection of asset IDs from Immich's web UI and return an opaque token.

        Deliberately **not** session-authenticated. The call is cross-origin from Immich's page,
        where the hub's SameSite=lax cookie is not sent, so requiring a session would only mean it
        never works. What guards it instead: the CORS allowlist is exactly ``EMBED_ORIGIN``, the
        rate limiter applies, the body is capped, and the token this returns is worthless without a
        logged-in hub session to exchange it. Nothing here reads or reveals anything.
        """
        if not settings.embed_origin:
            raise HTTPException(status_code=503, detail="EMBED_ORIGIN is not configured")

        origin = request.headers.get("origin", "")
        if origin and origin.rstrip("/") != settings.embed_origin:
            log.warning("rejected an inbox post from an unexpected origin")
            raise HTTPException(status_code=403, detail="origin not allowed")

        client = request.client.host if request.client else "unknown"
        if not limiter.allow(f"inbox:{client}"):
            raise HTTPException(status_code=429, detail="too many inbox requests")

        payload = await _json_or_form(request)
        raw = payload.get("asset_ids") or payload.get("assetIds") or []
        if not isinstance(raw, list):
            raise HTTPException(status_code=422, detail="asset_ids must be a list")

        try:
            selection = selections.create([str(a) for a in raw])
        except SelectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # The token and the count, never the IDs.
        return JSONResponse(
            {"token": selection.token, "count": selection.count, "ttl_s": int(selections.ttl_s)},
            status_code=201,
        )

    @app.get("/api/selection/{token}", dependencies=[Depends(require_login)])
    async def read_selection(token: str) -> JSONResponse:
        """Exchange a token for its asset IDs. Requires a hub session; this is the read side."""
        selection = selections.resolve(token)
        if selection is None:
            raise HTTPException(status_code=404, detail="that selection has expired")
        return JSONResponse({"asset_ids": list(selection.asset_ids), "count": selection.count})

    # --- webhook ------------------------------------------------------------------------

    async def _dispatch(request: Request, token: str) -> JSONResponse:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(status_code=429, detail="too many hook requests")

        expected = settings.hub_webhook_token
        if not expected:
            raise HTTPException(status_code=503, detail="HUB_WEBHOOK_TOKEN is not configured")
        # Never log either value; compare in constant time.
        if not hmac.compare_digest(token, expected):
            log.warning("rejected a webhook with a bad token from %s", client)
            raise HTTPException(status_code=401, detail="bad token")

        event = await _json_or_form(request)
        queued: list[int] = []
        for entry in catalog:
            if entry.addon is None or not store.is_enabled(entry.id):
                continue
            if not entry.addon.supports("webhook"):
                continue
            queued.append(jobs.enqueue(entry.id, {"trigger": "webhook", "event": event}))
        return JSONResponse({"queued": queued}, status_code=202)

    @app.post("/hooks/immich")
    async def hook_header(request: Request) -> JSONResponse:
        return await _dispatch(request, request.headers.get("x-addons-token", ""))

    @app.post("/hooks/immich/{token}")
    async def hook_path(request: Request, token: str) -> JSONResponse:
        """Fallback for when Immich's webhook step cannot send custom headers.

        The token is in the path, never in a query string, and is not logged.
        """
        return await _dispatch(request, token)

    return app


# --- helpers ----------------------------------------------------------------------------


def _session_secret(settings: Settings) -> str:
    """Stable per-install session key, so restarting the hub does not log everyone out."""
    path = settings.data_dir / ".session_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    return key


def _multi(form: Any) -> dict[str, list[str]]:
    return {key: [str(v) for v in form.getlist(key)] for key in form}


async def _json_or_form(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {"payload": body}
    form = await request.form()
    return {key: form[key] for key in form}


def _readable(error: dict[str, Any]) -> str:
    location = ".".join(str(p) for p in error.get("loc", ())) or "config"
    return f"{location}: {error.get('msg', 'invalid')}"


def _picker_values(settings: Settings) -> dict[str, list[str]]:
    """Values for the file pickers the schemas ask for (``x-picker``).

    Immich-backed pickers (albums, people) are filled in by the addon phases that need them; the
    file pickers work today because they are just directory listings.
    """

    def _listing(directory: Path, suffixes: set[str]) -> list[str]:
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if p.suffix.lower() in suffixes)

    return {
        "luts": _listing(settings.luts_dir, {".cube"}),
        "music": _listing(settings.music_dir, {".mp3", ".m4a", ".flac", ".wav", ".ogg"}),
        "albums": [],
        "people": [],
    }


app = None  # created by main(); import-time side effects are avoided for testability


def main() -> None:  # pragma: no cover - entry point
    import uvicorn

    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.hub_port)  # noqa: S104
