"""auto-lut — apply a .cube LUT to new assets and stack the graded copy with the original.

PLAN.md §6.1. Two trigger paths, same pipeline:

* ``poll`` — ask Immich for assets newer than our cursor. Depends only on the stable REST API, so
  it keeps working when Workflows changes;
* ``webhook`` — Immich's Workflows step posts an event; :meth:`AutoLut.on_event` pulls asset ids
  out of it. Workflows is a preview feature, so the parser is written to accept several shapes and
  to say clearly when it recognises none.

**Why the graded copy cannot re-trigger this addon.** Four independent guards, in order:

1. the copy is tagged ``addon:auto-lut``, and anything carrying an ``addon:*`` tag is skipped;
2. its filename matches ``auto-lut_<source-id>_<hash>``, and that pattern is skipped;
3. the source id of every copy we make is written to a state file, and a source that already has a
   copy is skipped;
4. the copy is a JPEG produced by us, so even a scope that admits it would still hit 1–3.

Any one of them would do. Together they mean a feedback loop needs four simultaneous failures.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field

from immich_addons.addons.base import Addon, AddonConfig, AddonError
from immich_addons.core import media
from immich_addons.core.client import ImmichClient
from immich_addons.core.config import Settings, get_settings
from immich_addons.core.jobs import JobContext

log = logging.getLogger(__name__)

ADDON_ID = "auto-lut"
TAG = f"addon:{ADDON_ID}"

#: Filenames we produce: ``auto-lut_<source asset id>_<config hash>.<ext>``. The id part is left
#: permissive on purpose — it must keep matching if Immich ever changes its id format.
UPLOAD_NAME = re.compile(r"^auto-lut_[\w-]+_[0-9a-f]{8}\.", re.IGNORECASE)

#: Skipped in v1 with a logged reason — grading a RAW properly needs a demosaic step first.
RAW_EXTENSIONS = frozenset(
    {"arw", "cr2", "cr3", "nef", "dng", "raf", "orf", "rw2", "pef", "srw", "3fr"}
)

VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "avi", "mkv", "webm", "mts", "m2ts"})


class AutoLutConfig(AddonConfig):
    lut: str = Field(
        default="",
        title="LUT",
        description="A .cube file in /data/luts.",
        json_schema_extra={"x-picker": "luts"},
    )
    intensity: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        title="Intensity",
        description="Blend of the graded image over the original. 1.0 is the LUT at full strength.",
    )
    album_ids: list[str] = Field(
        default_factory=list,
        title="Limit to albums",
        description="Leave empty to process every new asset.",
    )
    camera_models: list[str] = Field(
        default_factory=list,
        title="Limit to camera models",
        description="Matched against the asset's EXIF model, case-insensitively.",
    )
    extensions: list[str] = Field(
        default_factory=lambda: ["jpg", "jpeg", "heic"],
        title="File extensions",
        description="RAW is skipped in v1 regardless of what is listed here.",
    )
    process_videos: bool = Field(
        default=False,
        title="Process videos",
        description="Videos are re-encoded, which is slow. Off by default.",
    )
    stack_original: bool = Field(
        default=True,
        title="Stack with the original",
        description="Keeps the pair together in Immich instead of showing two separate assets.",
    )
    poll_limit: int = Field(
        default=200,
        ge=1,
        le=5000,
        title="Assets per poll",
        description="Upper bound on how many assets one polling run will look at.",
    )


@dataclass
class Skip:
    """A decision not to process an asset, with the reason that goes in the job log."""

    reason: str


def config_hash(config: AutoLutConfig) -> str:
    """Identifies the *look*. Changing the LUT or intensity produces a different graded file."""
    payload = json.dumps({"lut": config.lut, "intensity": config.intensity}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def graded_filename(asset_id: str, config: AutoLutConfig, suffix: str = ".jpg") -> str:
    return f"{ADDON_ID}_{asset_id}_{config_hash(config)}{suffix}"


def is_our_upload(filename: str) -> bool:
    return bool(UPLOAD_NAME.match(filename or ""))


def extension_of(filename: str) -> str:
    return Path(filename or "").suffix.lstrip(".").lower()


def has_addon_tag(asset: dict[str, Any]) -> bool:
    for tag in asset.get("tags") or []:
        name = tag.get("name") if isinstance(tag, dict) else str(tag)
        if str(name or "").startswith("addon:"):
            return True
    return False


def should_process(
    asset: dict[str, Any],
    config: AutoLutConfig,
    *,
    album_asset_ids: set[str] | None = None,
    already_done: set[str] | None = None,
) -> Skip | None:
    """The idempotency and scope guards, as one pure function.

    Returns ``None`` to process, or a :class:`Skip` carrying the reason. Pure so the guards can be
    tested exhaustively without an Immich server — they are the part that must never regress.
    """
    asset_id = str(asset.get("id", ""))
    filename = str(asset.get("originalFileName") or asset.get("originalPath") or "")
    extension = extension_of(filename)

    if has_addon_tag(asset):
        return Skip("already carries an addon:* tag")
    if is_our_upload(filename):
        return Skip("matches our own upload filename pattern")
    if already_done is not None and asset_id in already_done:
        return Skip("already graded with this LUT and intensity")

    is_video = str(asset.get("type", "")).upper() == "VIDEO" or extension in VIDEO_EXTENSIONS
    if extension in RAW_EXTENSIONS:
        return Skip(f"RAW ({extension}) is not supported in v1")
    if is_video and not config.process_videos:
        return Skip("videos are disabled in this addon's configuration")
    if (
        not is_video
        and config.extensions
        and extension not in {e.lower().lstrip(".") for e in config.extensions}
    ):
        return Skip(f"extension {extension!r} is outside the configured scope")

    if config.camera_models:
        model = str((asset.get("exifInfo") or {}).get("model") or "")
        wanted = {m.strip().lower() for m in config.camera_models if m.strip()}
        if model.lower() not in wanted:
            return Skip(f"camera {model!r} is outside the configured scope")

    if config.album_ids:
        if album_asset_ids is None:
            return Skip("album scope is configured but the album contents could not be read")
        if asset_id not in album_asset_ids:
            return Skip("not in any of the configured albums")

    return None


def asset_ids_from_event(event: dict[str, Any]) -> list[str]:
    """Pull asset ids out of an Immich Workflows webhook payload.

    Workflows is a preview feature and its payload shape is **not** guaranteed. This accepts the
    shapes seen so far and returns an empty list otherwise, so an unrecognised payload is a logged
    no-op rather than a crash. See ``tests/fixtures/webhook_asset_create.json`` and
    ``docs/workflow-setup.md``.
    """
    found: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 6 or len(found) > 500:
            return
        if isinstance(node, dict):
            for key in ("assetId", "asset_id"):
                if isinstance(node.get(key), str):
                    found.append(node[key])
            asset = node.get("asset")
            if isinstance(asset, dict) and isinstance(asset.get("id"), str):
                found.append(asset["id"])
            if isinstance(node.get("id"), str) and {"type", "originalFileName"} & node.keys():
                found.append(node["id"])
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                visit(item, depth + 1)

    visit(event)
    return list(dict.fromkeys(found))  # de-duplicated, order preserved


class State:
    """What this addon has already done, so a repeat event is a no-op.

    Kept as one small JSON file under ``$DATA_DIR/cache`` rather than in Immich, so idempotency
    does not depend on being able to search for our own uploads.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"graded": {}, "cursor": ""}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("auto-lut state at %s is corrupt; starting fresh", path)
        self.data.setdefault("graded", {})
        self.data.setdefault("cursor", "")

    def key(self, asset_id: str, config: AutoLutConfig) -> str:
        return f"{asset_id}:{config_hash(config)}"

    def done_ids(self, config: AutoLutConfig) -> set[str]:
        suffix = f":{config_hash(config)}"
        return {k.split(":", 1)[0] for k in self.data["graded"] if k.endswith(suffix)}

    def record(self, asset_id: str, config: AutoLutConfig, graded_id: str, filename: str) -> None:
        self.data["graded"][self.key(asset_id, config)] = {
            "graded_id": graded_id,
            "filename": filename,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self.save()

    @property
    def cursor(self) -> str:
        return str(self.data.get("cursor") or "")

    @cursor.setter
    def cursor(self, value: str) -> None:
        self.data["cursor"] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)


class AutoLut(Addon):
    id: ClassVar[str] = ADDON_ID
    name: ClassVar[str] = "Auto-LUT"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = (
        "Applies a chosen .cube LUT to new photos/videos and stacks the graded copy "
        "with the original."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("webhook", "poll")
    config_model: ClassVar[type[AddonConfig]] = AutoLutConfig

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # --- triggers ------------------------------------------------------------------------

    def on_event(self, event: dict[str, Any], config: AddonConfig) -> list[dict[str, Any]]:
        return [{"asset_id": asset_id} for asset_id in asset_ids_from_event(event)]

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        assert isinstance(config, AutoLutConfig)
        lut_path = self._lut_path(config)
        state = State(self.settings.cache_dir / "auto_lut_state.json")

        with ImmichClient(self.settings, dry_run=config.dry_run) as client:
            assets = self._assets_for(ctx, client, config, state)
            if not assets:
                ctx.log("nothing to do")
                return

            album_asset_ids = self._album_asset_ids(client, config)
            done = state.done_ids(config)
            processed = skipped = 0

            for i, asset in enumerate(assets):
                ctx.progress(i / len(assets), "")
                skip = should_process(
                    asset, config, album_asset_ids=album_asset_ids, already_done=done
                )
                if skip is not None:
                    ctx.log(f"skip {asset.get('originalFileName', asset.get('id'))}: {skip.reason}")
                    skipped += 1
                    continue
                self._grade_one(ctx, client, asset, config, lut_path, state)
                processed += 1

            if config.dry_run:
                ctx.log(f"dry run: {len(client.dry_run_calls)} write(s) were logged, none sent")
            ctx.log(f"graded {processed}, skipped {skipped}")

    # --- pipeline ------------------------------------------------------------------------

    def _grade_one(
        self,
        ctx: JobContext,
        client: ImmichClient,
        asset: dict[str, Any],
        config: AutoLutConfig,
        lut_path: Path,
        state: State,
    ) -> None:
        asset_id = str(asset["id"])
        original_name = str(asset.get("originalFileName") or asset_id)
        is_video = str(asset.get("type", "")).upper() == "VIDEO"
        suffix = Path(original_name).suffix if is_video else ".jpg"
        out_name = graded_filename(asset_id, config, suffix=suffix or ".jpg")

        with tempfile.TemporaryDirectory(prefix="auto-lut-") as tmp:
            work = Path(tmp)
            source = client.download_original(asset_id, work / f"src_{original_name}")
            graded = work / out_name

            ctx.log(f"grading {original_name} with {lut_path.name} at {config.intensity}")
            media.apply_lut(source, graded, lut_path, intensity=config.intensity, is_video=is_video)
            media.copy_exif(source, graded)

            uploaded = client.upload_asset(graded, device_asset_id=graded.stem)

        graded_id = str(uploaded.get("id") or "")
        if not graded_id:
            # Dry run: there is no new asset, so there is nothing to tag or stack. Say so plainly
            # rather than pretending the rest of the pipeline ran.
            ctx.log(f"dry run: would upload {out_name}, then tag {TAG} and stack with {asset_id}")
            return

        self._tag(ctx, client, graded_id)
        if config.stack_original:
            client.create_stack([asset_id, graded_id])
            ctx.log(f"stacked {graded_id} with {asset_id}")

        state.record(asset_id, config, graded_id, out_name)
        ctx.artifact("asset", graded_id, f"{original_name} graded with {lut_path.name}")
        ctx.log(f"uploaded {out_name} as {graded_id}")

    def _tag(self, ctx: JobContext, client: ImmichClient, asset_id: str) -> None:
        tag_id = ""
        for tag in client.tags():
            if str(tag.get("name") or tag.get("value") or "") in {TAG, TAG.split(":")[-1]}:
                tag_id = str(tag.get("id") or "")
                break
        if not tag_id:
            created = client.create_tag(TAG)
            tag_id = str(created.get("id") or "")
        if tag_id:
            client.assign_tag(tag_id, [asset_id])
        else:
            ctx.log(f"could not resolve the {TAG} tag — the upload is untagged")

    # --- inputs --------------------------------------------------------------------------

    def _assets_for(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: AutoLutConfig,
        state: State,
    ) -> list[dict[str, Any]]:
        event = ctx.params.get("event")
        if event:
            ids = asset_ids_from_event(event)
            if not ids:
                ctx.log(
                    "the webhook payload contained no asset id — check docs/workflow-setup.md, "
                    "and consider TRIGGER_MODE=poll if Immich changed the payload"
                )
                return []
            ctx.log(f"webhook: {len(ids)} asset(s)")
            return [client.asset_info(asset_id) for asset_id in ids]

        if ctx.params.get("asset_id"):
            return [client.asset_info(str(ctx.params["asset_id"]))]

        return self._poll(ctx, client, config, state)

    def _poll(
        self,
        ctx: JobContext,
        client: ImmichClient,
        config: AutoLutConfig,
        state: State,
    ) -> list[dict[str, Any]]:
        cursor = state.cursor
        ctx.log(f"polling for assets since {cursor or 'the beginning'}")
        taken_after = datetime.fromisoformat(cursor) if cursor else None

        assets: list[dict[str, Any]] = []
        for asset in client.iter_metadata(taken_after=taken_after):
            assets.append(asset)
            if len(assets) >= config.poll_limit:
                ctx.log(f"stopping at the {config.poll_limit} asset limit for this run")
                break

        newest = max(
            (str(a.get("fileCreatedAt") or "") for a in assets),
            default="",
        )
        if newest:
            state.cursor = newest
            state.save()
        return assets

    def _album_asset_ids(self, client: ImmichClient, config: AutoLutConfig) -> set[str] | None:
        if not config.album_ids:
            return None
        ids: set[str] = set()
        for album_id in config.album_ids:
            album = client.album_info(album_id)
            for asset in album.get("assets", []):
                if isinstance(asset, dict) and asset.get("id"):
                    ids.add(str(asset["id"]))
        return ids

    def _lut_path(self, config: AutoLutConfig) -> Path:
        if not config.lut:
            raise AddonError("no LUT selected — choose one in the addon's configuration")
        # Reject anything that would climb out of the LUT directory.
        candidate = (self.settings.luts_dir / Path(config.lut).name).resolve()
        if not candidate.is_file():
            raise AddonError(f"LUT {config.lut!r} not found in {self.settings.luts_dir}")
        return candidate
