"""Tests for generated definitions.

Ensures generated entities expose valid ``get``/``set`` access strings and
unique enum value labels.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from pyhems import REGISTRY, EntityDefinition, PropertyRole

ALLOWED_ACCESS_VALUES = {
    "required",
    "required_c",
    "required_o",
    "optional",
    "notApplicable",
}


def _all_entities() -> Iterator[EntityDefinition]:
    for entities in REGISTRY.entities.values():
        yield from entities


def test_all_entities_have_get_set_strings() -> None:
    for ent in _all_entities():
        assert isinstance(ent.get, str), f"'get' must be string for {ent.id}"
        assert isinstance(ent.set, str), f"'set' must be string for {ent.id}"
        assert ent.get in ALLOWED_ACCESS_VALUES, (
            f"Unexpected get value: {ent.get} in {ent.id}"
        )
        assert ent.set in ALLOWED_ACCESS_VALUES, (
            f"Unexpected set value: {ent.set} in {ent.id}"
        )


def test_enum_values_are_unique() -> None:
    for ent in _all_entities():
        if not ent.enum_values:
            continue
        names_en = [v.name_en for v in ent.enum_values]
        assert len(names_en) == len(set(names_en)), (
            f"Duplicate 'name_en' in enum_values of {ent.id}: {names_en}"
        )
        names_ja = [v.name_ja for v in ent.enum_values]
        assert len(names_ja) == len(set(names_ja)), (
            f"Duplicate 'name_ja' in enum_values of {ent.id}: {names_ja}"
        )


def test_common_operation_status_access() -> None:
    """Operation status (0x80) is a common superClass property on every device."""
    entities = {e.epc: e for e in REGISTRY.entities[0x0602]}
    op = entities[0x80]
    assert op.get == "required"
    assert op.set == "optional"
    mra_tv = Path("mra/devices/0x0602.json")
    assert mra_tv.exists(), "MRA file for TV must exist"


def test_create_numeric_encoder_uint16_with_scale() -> None:
    """NumericCodec should reverse scale."""
    from pyhems import NumericCodec

    codec = NumericCodec(
        mra_format="uint16", scale=0.1, minimum=None, maximum=None, byte_offset=0
    )
    assert codec.encode(12.3) == b"\x00{"


def test_create_numeric_encoder_out_of_range_raises_value_error() -> None:
    """NumericCodec should reject values outside format range."""
    from pyhems import NumericCodec

    codec = NumericCodec(
        mra_format="uint8", scale=1.0, minimum=None, maximum=None, byte_offset=0
    )
    with pytest.raises(ValueError, match="out of range"):
        codec.encode(300)


def test_object_property_is_split_into_flat_entities() -> None:
    """Class 0x0287 EPC 0xD0 (kWh + R/T current) splits by byte_offset.

    Confirms the fixed-layout MRA "object" splitting feature: one EPC that
    packs multiple scalar fields into a single EDT (measurement channel 1:
    cumulative kWh + two instantaneous currents) becomes three flat
    EntityDefinition entries instead of being dropped entirely.
    """
    entities = {e.id: e for e in REGISTRY.entities[0x0287] if e.epc == 0xD0}
    assert set(entities) == {
        "class_0287_epc_d0",
        "class_0287_epc_d0_04",
        "class_0287_epc_d0_06",
    }

    kwh = entities["class_0287_epc_d0"]
    assert kwh.byte_offset == 0
    assert kwh.format == "uint32"
    assert kwh.unit == "kWh"
    assert kwh.coefficient_epcs == (0xC2,)

    current_r = entities["class_0287_epc_d0_04"]
    assert current_r.byte_offset == 4
    assert current_r.format == "int16"
    assert current_r.unit == "A"
    assert current_r.coefficient_epcs is None

    current_t = entities["class_0287_epc_d0_06"]
    assert current_t.byte_offset == 6
    assert current_t.format == "int16"


def test_coefficient_reference_epc_gets_numeric_values() -> None:
    """EPC 0xC2 (unit for cumulative electric energy) becomes a numeric_values table."""
    entities = {e.epc: e for e in REGISTRY.entities[0x0287]}
    unit_entity = entities[0xC2]
    assert unit_entity.format is None
    assert unit_entity.enum_values == ()
    assert unit_entity.numeric_values
    values_by_edt = {nv.edt: nv.value for nv in unit_entity.numeric_values}
    assert values_by_edt[0x00] == 1
    assert values_by_edt[0x01] == 0.1
    assert values_by_edt[0x0D] == 10000


def test_scalar_cumulative_energy_carries_coefficient_epcs() -> None:
    """EPC 0xC0/0xC1 (cumulative energy) reference EPC 0xC2 as their unit."""
    entities = {e.epc: e for e in REGISTRY.entities[0x0287]}
    assert entities[0xC0].coefficient_epcs == (0xC2,)
    assert entities[0xC1].coefficient_epcs == (0xC2,)

    # Class 0x0288 (low-voltage smart meter) references two coefficient EPCs.
    smart_meter_entities = {e.epc: e for e in REGISTRY.entities[0x0288]}
    assert smart_meter_entities[0xE0].coefficient_epcs == (0xD3, 0xE1)


def test_atomic_paired_range_selector_is_not_split() -> None:
    """EPC 0xB2 (paired with the unsupported 0xB3 channel list) stays dropped.

    Object splitting must not expose a channel-range-selector config entity
    for a list result (EPC 0xB3) that pyhems does not yet support decoding.
    """
    epcs = {e.epc for e in REGISTRY.entities[0x0287]}
    assert 0xB2 not in epcs
    assert 0xB3 not in epcs


def _entity(class_code: int, epc: int) -> EntityDefinition:
    return next(e for e in REGISTRY.entities[class_code] if e.epc == epc)


def test_role_defaults_to_primary() -> None:
    """A property absent from property_roles.xlsx defaults to PRIMARY."""
    assert _entity(0x0130, 0xBB).role is PropertyRole.PRIMARY  # room temperature


def test_curated_diagnostic_property_has_status_role() -> None:
    """A property curated in property_roles.xlsx as DIAGNOSTIC maps to STATUS."""
    assert _entity(0x026F, 0xE7).role is PropertyRole.STATUS  # battery level


def test_curated_config_property_has_setting_role() -> None:
    """A property curated in property_roles.xlsx as CONFIG maps to SETTING."""
    assert _entity(0x0130, 0x87).role is PropertyRole.SETTING  # current limit


def test_common_instantaneous_power_has_instantaneous_role() -> None:
    """A common fast-changing measurement is curated as INSTANTANEOUS."""
    assert _entity(0x0130, 0x84).role is PropertyRole.INSTANTANEOUS


def test_writable_instantaneous_named_setting_is_not_instantaneous_role() -> None:
    """Regression: a writable setting whose name contains "instantaneous".

    Must not be misclassified as a fast-poll INSTANTANEOUS role, even though
    a name-only heuristic would match it.
    """
    entity = _entity(0x02A7, 0xCC)
    assert entity.set != "notApplicable"
    assert entity.role is PropertyRole.SETTING


if __name__ == "__main__":
    pytest.main([__file__])
