"""The addon contract.

An addon is a small class that declares what it can do, a pydantic model for its configuration,
and a :meth:`Addon.run` that does the work through a :class:`~immich_addons.core.jobs.JobContext`.
The hub discovers addons through the ``immich_addons.addons`` entry-point group, so a third-party
package can ship one without this repo knowing about it.

Two things every addon gets for free by inheriting :class:`Addon`:

* ``dry_run`` is a field on every config model (:class:`AddonConfig`), defaulting to true;
* the JSON Schema that drives the hub's config form is generated from that model, so the form and
  the validation can never drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal, get_args

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from immich_addons.core.jobs import JobContext

Capability = Literal["webhook", "poll", "manual", "schedule"]
CAPABILITIES: tuple[str, ...] = get_args(Capability)


class AddonConfig(BaseModel):
    """Base for every addon's config model.

    ``dry_run`` lives here rather than in each addon so it cannot be forgotten: CLAUDE.md requires
    every addon to default to dry run until it is explicitly turned off.
    """

    model_config = {"extra": "forbid"}

    dry_run: bool = Field(
        default=True,
        title="Dry run",
        description=(
            "Run the whole pipeline but log intended writes to Immich instead of performing them."
        ),
    )


class AddonError(RuntimeError):
    """Raised by an addon for a failure that is expected and explainable in the job log."""


class Addon:
    """Base class for addons. Subclasses set the class attributes and implement :meth:`run`."""

    #: Stable identifier, matching the ``id`` in ``registry/index.json``.
    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    version: ClassVar[str] = "0.0.0"
    description: ClassVar[str] = ""
    capabilities: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[AddonConfig]] = AddonConfig

    # --- configuration -------------------------------------------------------------------

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        """JSON Schema for this addon's config — the single source for the hub's form."""
        return cls.config_model.model_json_schema()

    @classmethod
    def parse_config(cls, raw: dict[str, Any] | None) -> AddonConfig:
        """Validate stored/submitted config. Raises ``pydantic.ValidationError`` when invalid."""
        return cls.config_model.model_validate(raw or {})

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """Config with every default filled in, used when an addon has never been configured.

        An empty config is not necessarily *valid* — zine-maker demands a topic or an album, so
        instantiating the model would raise. In that case fall back to the per-field defaults out
        of the JSON Schema, which is exactly what the form wants to prefill anyway.
        """
        try:
            return cls.config_model().model_dump(mode="json")
        except ValidationError:
            properties = cls.config_schema().get("properties", {})
            return {name: spec["default"] for name, spec in properties.items() if "default" in spec}

    # --- behaviour -----------------------------------------------------------------------

    def run(self, ctx: JobContext, config: AddonConfig) -> None:
        """Do the work. Report through ``ctx``; raise to fail the job."""
        raise NotImplementedError(f"addon {self.id!r} does not implement run()")

    def on_event(self, event: dict[str, Any], config: AddonConfig) -> list[dict[str, Any]]:
        """Turn an Immich webhook/poll event into zero or more job parameter dicts.

        Returning an empty list means "not interested" — that is how an addon filters out assets
        outside its scope before a job is ever created.
        """
        raise NotImplementedError(f"addon {self.id!r} does not implement on_event()")

    # --- helpers -------------------------------------------------------------------------

    @property
    def tag(self) -> str:
        """The Immich tag every asset this addon uploads must carry."""
        return f"addon:{self.id}"

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities
