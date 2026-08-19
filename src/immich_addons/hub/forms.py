"""JSON Schema -> HTML form -> validated config.

This is the reusable heart of the hub (PLAN.md §5): an addon declares a pydantic model, the model
generates a JSON Schema, and this module turns that schema into form fields and the submitted
strings back into typed values. Nothing about any specific addon appears here, so a third-party
addon gets a working config page for free.

Only the subset of JSON Schema that pydantic emits for our config models is handled: objects of
scalars, ``enum``, arrays of strings, and ``anyOf`` with null for optional values. Anything else
falls back to a text input rather than silently dropping the field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Widgets the templates know how to render.
Widget = str  # "text" | "textarea" | "number" | "checkbox" | "select" | "tags" | "date"


@dataclass
class FormField:
    name: str
    label: str
    widget: Widget
    value: Any = None
    description: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    step: str | None = None
    required: bool = False
    picker: str = ""

    @property
    def value_str(self) -> str:
        if self.value is None:
            return ""
        if isinstance(self.value, list):
            return ", ".join(str(v) for v in self.value)
        return str(self.value)


def _unwrap_optional(spec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """``date | None`` arrives as ``anyOf: [{...}, {type: null}]``. Return the real branch."""
    branches = spec.get("anyOf") or spec.get("oneOf")
    if not branches:
        return spec, False
    real = [b for b in branches if b.get("type") != "null"]
    optional = len(real) != len(branches)
    if not real:
        return spec, optional
    merged = dict(real[0])
    for key in ("title", "description", "default"):
        if key in spec:
            merged[key] = spec[key]
    return merged, optional


def _label(name: str, spec: dict[str, Any]) -> str:
    return spec.get("title") or name.replace("_", " ").capitalize()


def _options(spec: dict[str, Any]) -> list[tuple[str, str]]:
    return [(str(v), str(v).replace("_", " ")) for v in spec.get("enum", [])]


def _widget_for(spec: dict[str, Any]) -> Widget:
    if spec.get("enum"):
        return "select"
    kind = spec.get("type")
    if kind == "boolean":
        return "checkbox"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "array":
        return "tags"
    if kind == "string" and spec.get("format") == "date":
        return "date"
    if kind == "string" and spec.get("x-multiline"):
        return "textarea"
    return "text"


def schema_to_fields(
    schema: dict[str, Any], values: dict[str, Any] | None = None
) -> list[FormField]:
    """Turn a JSON Schema object into renderable fields, filled in from ``values``."""
    values = values or {}
    required = set(schema.get("required", []))
    fields: list[FormField] = []

    for name, raw_spec in schema.get("properties", {}).items():
        spec, optional = _unwrap_optional(raw_spec)
        widget = _widget_for(spec)
        value = values.get(name, spec.get("default"))

        fields.append(
            FormField(
                name=name,
                label=_label(name, raw_spec if raw_spec.get("title") else spec),
                widget=widget,
                value=value,
                description=raw_spec.get("description") or spec.get("description", ""),
                options=_options(spec),
                minimum=spec.get("minimum", spec.get("exclusiveMinimum")),
                maximum=spec.get("maximum", spec.get("exclusiveMaximum")),
                step="any" if spec.get("type") == "number" else None,
                required=name in required and not optional,
                picker=str(raw_spec.get("x-picker") or spec.get("x-picker") or ""),
            )
        )
    return fields


def _coerce(spec: dict[str, Any], raw: list[str]) -> Any:
    """Turn submitted form strings into the type the schema asks for.

    Coercion is deliberately forgiving — pydantic does the real validation afterwards and produces
    the error message the user sees.
    """
    spec, _ = _unwrap_optional(spec)
    kind = spec.get("type")

    if kind == "boolean":
        # An unchecked checkbox submits nothing at all.
        return bool(raw) and raw[-1] not in {"", "false", "off", "0"}
    if not raw:
        return None
    text = raw[-1].strip()

    if kind == "array":
        items = [part.strip() for part in text.split(",") if part.strip()]
        item_type = (spec.get("items") or {}).get("type")
        if item_type == "integer":
            return [int(i) for i in items]
        if item_type == "number":
            return [float(i) for i in items]
        return items
    if text == "":
        return None
    if kind == "integer":
        return int(float(text))
    if kind == "number":
        return float(text)
    if spec.get("enum") and all(isinstance(v, int) for v in spec["enum"]):
        return int(text)
    return text


def form_to_dict(schema: dict[str, Any], form: dict[str, list[str]]) -> dict[str, Any]:
    """Build a config dict from submitted form data, ready for pydantic validation.

    Fields absent from the submission are left out entirely (so the model's default applies),
    except booleans, where absence means *unchecked*.
    """
    out: dict[str, Any] = {}
    for name, spec in schema.get("properties", {}).items():
        unwrapped, _ = _unwrap_optional(spec)
        present = name in form
        if not present and unwrapped.get("type") != "boolean":
            continue
        value = _coerce(spec, form.get(name, []))
        if value is None and not present:
            continue
        out[name] = value
    return out
