"""Tests for :class:`EntityDefinition`'s custom compact ``__repr__``."""

from pyhems import EntityDefinition, NumericValueEntry, PropertyRole


def _make_entity(**overrides: object) -> EntityDefinition:
    defaults: dict[str, object] = {
        "id": "class_0287_epc_d0",
        "epc": 0xD0,
        "name_en": "x",
        "name_ja": "y",
        "get": "required",
        "set": "notApplicable",
    }
    defaults.update(overrides)
    return EntityDefinition(**defaults)  # type: ignore[arg-type]


def test_role_defaults_to_primary() -> None:
    """Entities without a curated role default to PRIMARY."""
    assert _make_entity().role is PropertyRole.PRIMARY


def test_property_role_repr_is_valid_source() -> None:
    """PropertyRole must repr as importable source for the generated module."""
    assert repr(PropertyRole.SETTING) == "PropertyRole.SETTING"


def test_repr_omits_default_valued_fields() -> None:
    """Fields left at their declared default are not rendered."""
    entity = _make_entity()
    text = repr(entity)
    assert text == (
        "EntityDefinition(id='class_0287_epc_d0', epc=208, name_en='x', "
        "name_ja='y', get='required', set='notApplicable')"
    )
    assert "format=" not in text
    assert "enum_values=" not in text
    assert "multiple_of=" not in text
    assert "byte_offset=" not in text
    assert "numeric_values=" not in text
    assert "coefficient_epcs=" not in text


def test_repr_includes_non_default_fields_only() -> None:
    """Non-default fields are rendered; default-valued ones are still omitted."""
    entity = _make_entity(
        format="uint32",
        unit="kWh",
        coefficient_epcs=(0xC2,),
    )
    text = repr(entity)
    assert "format='uint32'" in text
    assert "unit='kWh'" in text
    assert "coefficient_epcs=(194,)" in text
    assert "enum_values=" not in text
    assert "multiple_of=" not in text
    assert "numeric_values=" not in text


def test_repr_round_trips_via_eval() -> None:
    """``eval(repr(entity))`` reconstructs an equal EntityDefinition."""
    entity = _make_entity(
        numeric_values=(NumericValueEntry(edt=0, value=1.0),),
    )
    rebuilt = eval(
        repr(entity),
        {"EntityDefinition": EntityDefinition, "NumericValueEntry": NumericValueEntry},
    )
    assert rebuilt == entity


def test_repr_with_non_default_role_round_trips_via_eval() -> None:
    """A non-default role is rendered and round-trips via eval()."""
    entity = _make_entity(role=PropertyRole.SETTING)
    text = repr(entity)
    assert "role=PropertyRole.SETTING" in text
    rebuilt = eval(
        text,
        {"EntityDefinition": EntityDefinition, "PropertyRole": PropertyRole},
    )
    assert rebuilt == entity
