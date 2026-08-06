"""JSON Schema -> form -> validated config, the reusable core of the hub."""

from __future__ import annotations

import pytest

from immich_addons.addons.auto_lut import AutoLut
from immich_addons.addons.trip_best_picks import TripBestPicks
from immich_addons.addons.year_highlights import YearHighlights
from immich_addons.addons.zine_maker import ZineMaker
from immich_addons.hub.forms import form_to_dict, schema_to_fields

ALL_ADDONS = [AutoLut, TripBestPicks, ZineMaker, YearHighlights]


def _by_name(fields):  # noqa: ANN001, ANN202
    return {f.name: f for f in fields}


@pytest.mark.parametrize("addon_cls", ALL_ADDONS)
def test_every_addon_generates_a_complete_form(addon_cls) -> None:  # noqa: ANN001
    fields = schema_to_fields(addon_cls.config_schema(), addon_cls.default_config())
    assert {f.name for f in fields} == set(addon_cls.config_schema()["properties"])
    assert all(f.widget for f in fields), "a field got no widget"


@pytest.mark.parametrize("addon_cls", ALL_ADDONS)
def test_dry_run_is_on_every_form_and_defaults_to_true(addon_cls) -> None:  # noqa: ANN001
    """CLAUDE.md: dry run is the default for every addon, and must be visible in the UI."""
    field = _by_name(schema_to_fields(addon_cls.config_schema(), addon_cls.default_config()))[
        "dry_run"
    ]
    assert field.widget == "checkbox"
    assert field.value is True


def test_widgets_match_the_declared_types() -> None:
    fields = _by_name(schema_to_fields(AutoLut.config_schema(), AutoLut.default_config()))
    assert fields["intensity"].widget == "number"
    assert fields["intensity"].minimum == 0.0
    assert fields["intensity"].maximum == 1.0
    assert fields["process_videos"].widget == "checkbox"
    assert fields["extensions"].widget == "tags"
    assert fields["lut"].picker == "luts"


def test_literals_become_selects() -> None:
    fields = _by_name(schema_to_fields(ZineMaker.config_schema(), ZineMaker.default_config()))
    assert fields["layout"].widget == "select"
    assert [value for value, _ in fields["layout"].options] == ["mini8", "booklet"]
    assert fields["pages"].widget == "select"
    assert [value for value, _ in fields["pages"].options] == ["8", "16"]


def test_optional_dates_unwrap_to_a_date_input() -> None:
    """`date | None` arrives as anyOf with a null branch; the form must still show a date."""
    fields = _by_name(
        schema_to_fields(TripBestPicks.config_schema(), TripBestPicks.default_config())
    )
    assert fields["date_from"].widget == "date"


def test_labels_and_hints_come_from_the_model() -> None:
    fields = _by_name(schema_to_fields(AutoLut.config_schema(), AutoLut.default_config()))
    assert fields["stack_original"].label == "Stack with the original"
    assert "Immich" in fields["stack_original"].description


def test_form_round_trips_through_pydantic() -> None:
    schema = AutoLut.config_schema()
    submitted = {
        "lut": ["kodak.cube"],
        "intensity": ["0.4"],
        "extensions": ["jpg, heic , dng"],
        "process_videos": ["on"],
        "album_ids": [""],
        "camera_models": [""],
        # stack_original and dry_run are unchecked, so the browser sends nothing for them
    }
    config = AutoLut.parse_config(form_to_dict(schema, submitted))

    assert config.lut == "kodak.cube"
    assert config.intensity == 0.4
    assert config.extensions == ["jpg", "heic", "dng"]
    assert config.process_videos is True
    assert config.stack_original is False, "an unchecked box must read as False, not as its default"
    assert config.dry_run is False


def test_integer_enums_coerce_back_to_int() -> None:
    schema = ZineMaker.config_schema()
    config = ZineMaker.parse_config(
        form_to_dict(schema, {"topic": ["beach"], "pages": ["16"], "layout": ["booklet"]})
    )
    assert config.pages == 16


def test_invalid_input_is_rejected_by_the_model_not_by_the_form() -> None:
    """The form coerces; pydantic decides. That keeps one source of truth for validation."""
    from pydantic import ValidationError

    values = form_to_dict(AutoLut.config_schema(), {"intensity": ["3"]})
    assert values["intensity"] == 3.0
    with pytest.raises(ValidationError):
        AutoLut.parse_config(values)


def test_cross_field_rules_are_enforced() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="mini8"):
        ZineMaker.parse_config({"topic": "beach", "layout": "mini8", "pages": 16})

    with pytest.raises(ValidationError, match="album_id"):
        TripBestPicks.parse_config({"source": "album"})


def test_absent_non_boolean_fields_fall_back_to_the_model_default() -> None:
    config = AutoLut.parse_config(form_to_dict(AutoLut.config_schema(), {"dry_run": ["on"]}))
    assert config.intensity == 1.0
    assert config.extensions == ["jpg", "jpeg", "heic"]
