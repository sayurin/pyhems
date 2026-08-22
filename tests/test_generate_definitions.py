"""Tests for generated definitions.

Ensures generated entities expose valid ``get``/``set`` access strings and
unique enum value labels.
"""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from pyhems import REGISTRY, EntityDefinition, PropertyRole, get_codec_for_epc
from pyhems._definitions_generated import _merge_entities
from pyhems.definitions import PropertyValueDefinition

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


def test_custom_define_uses_stable_scope_and_offset_ids() -> None:
    """Manufacturer-specific custom definitions include scope and offset in IDs."""
    entities = {
        entity.id: entity for entity in REGISTRY.entities[0x0135] if entity.epc == 0xF1
    }
    assert set(entities) == {
        "class_0135_epc_f1_000005_01",
        "class_0135_epc_f1_000005_03",
        "class_0135_epc_f1_000005_04",
        "class_0135_epc_f1_000005_1c",
    }
    assert all(entity.manufacturer_code == 0x000005 for entity in entities.values())


def test_entity_groups_merge_with_later_epcs_winning() -> None:
    """Later entity groups replace all earlier entities for matching EPCs."""
    operation_status = _entity(0x0602, 0x80)
    instantaneous_power = _entity(0x0602, 0x84)
    replacement = replace(operation_status, id="replacement")

    assert _merge_entities((operation_status, instantaneous_power), (replacement,)) == (
        instantaneous_power,
        replacement,
    )


def test_custom_define_overrides_common_epc_for_its_class() -> None:
    """A custom class definition replaces the matching common EPC."""
    entities = [entity for entity in REGISTRY.entities[0x0F02] if entity.epc == 0x8F]
    assert len(entities) == 1
    entity = entities[0]
    assert entity.id == "class_0f02_epc_8f_000006"
    assert entity.manufacturer_code == 0x000006
    assert get_codec_for_epc(0x0F02, 0x8F).decode(b"\x42") is False


def test_custom_patch_preserves_unspecified_mra_values() -> None:
    """A label-only patch leaves the MRA Japanese label unchanged."""
    entity = _entity(0x0134, 0xE0)
    values = {value.key: value for value in entity.enum_values}
    assert values["false"].name_en == "Heat exchanger OFF"
    assert values["false"].name_ja == "熱交換機OFF"


@pytest.mark.parametrize(
    ("epc", "expected_keys"),
    [
        (0xA4, ("uppermost", "upperCenter", "central", "lowerCenter", "lowermost")),
        (
            0xA5,
            (
                "r",
                "rc",
                "c",
                "lc",
                "l",
                "rc_r",
                "c_r",
                "lc_r",
                "l_r",
                "c_rc",
                "lc_rc",
                "l_rc",
                "lc_c",
                "l_c",
                "l_lc",
                "c_rc_r",
                "lc_rc_r",
                "lc_c_r",
                "lc_c_rc",
                "l_rc_r",
                "l_c_r",
                "l_c_rc",
                "l_lc_r",
                "l_lc_rc",
                "l_lc_c",
                "lc_c_rc_r",
                "l_c_rc_r",
                "l_lc_rc_r",
                "l_lc_c_r",
                "l_lc_c_rc",
                "l_lc_c_rc_r",
            ),
        ),
    ],
)
def test_full_enum_patch_preserves_yaml_order(
    epc: int, expected_keys: tuple[str, ...]
) -> None:
    """A complete enum patch uses the curated YAML value order."""
    assert (
        tuple(value.key for value in _entity(0x0130, epc).enum_values) == expected_keys
    )


# ============================================================================
# Structured value definitions & collection bindings (class 0x0287 branch
# circuit metering arrays — see docs/ha-0287-epc-be-implementation-report-v2.md)
# ============================================================================


def test_structured_values_do_not_duplicate_scalar_entities() -> None:
    """An EPC is never both a flat EntityDefinition and a structured value.

    Properties fully represented by a scalar EntityDefinition must not also
    get a redundant PropertyValueDefinition tree.
    """
    for class_code, structured in REGISTRY.structured_values.items():
        entity_epcs = {e.epc for e in REGISTRY.entities.get(class_code, ())}
        overlap = entity_epcs & set(structured)
        assert not overlap, (
            f"class 0x{class_code:04X} EPCs both scalar and structured: "
            f"{[hex(e) for e in overlap]}"
        )


def test_0287_channel_count_epcs_are_plain_scalar_entities() -> None:
    """EPC 0xB1/0xB8 (channel counts) are plain sensors, not structured values."""
    entities = {e.epc: e for e in REGISTRY.entities[0x0287]}
    for epc in (0xB1, 0xB8):
        entity = entities[epc]
        assert entity.format == "uint8"
        assert entity.minimum == 1
        assert entity.maximum == 252
        assert entity.unit is None
        assert entity.enum_values == ()
        assert epc not in REGISTRY.structured_values.get(0x0287, {})


def test_0287_array_properties_preserved_as_structured_values() -> None:
    """Every 0x0287 property containing an MRA array is kept as a value tree.

    These EPCs cannot become flat EntityDefinitions (variable-length lists),
    but must not be silently dropped either.
    """
    expected = {0xB3, 0xB5, 0xB7, 0xBA, 0xBC, 0xBE, 0xC3, 0xC4}
    structured = REGISTRY.structured_values[0x0287]
    assert expected <= set(structured)
    for epc in expected:
        from pyhems.definitions import ArrayDefinition, ObjectDefinition

        value_def = structured[epc]
        assert isinstance(value_def, ObjectDefinition)
        assert any(isinstance(f.value, ArrayDefinition) for f in value_def.fields)


def test_0287_collection_bindings_match_v2_scope() -> None:
    """Only B3/B7/BA/BE get a curated CollectionBinding (v2 HA projection scope).

    B4/B5/B6/B9/BB/BC/BD (selectors + instantaneous current lists) are kept
    as structured values but intentionally get no CollectionBinding, since
    v2 does not project them onto HA entities.
    """
    bindings = {b.result_epc: b for b in REGISTRY.collection_bindings[0x0287]}
    assert set(bindings) == {0xB3, 0xB7, 0xBA, 0xBE}
    assert bindings[0xB3].count_epc == 0xB1
    assert bindings[0xB7].count_epc == 0xB1
    assert bindings[0xBA].count_epc == 0xB8
    assert bindings[0xBE].count_epc == 0xB8
    for binding in bindings.values():
        assert binding.start_path == ("startChannel",)
        assert binding.page_count_path == ("range",)


@pytest.mark.parametrize(
    ("class_code", "epc"),
    [(cc, b.result_epc) for cc, bs in REGISTRY.collection_bindings.items() for b in bs],
)
def test_every_collection_binding_has_a_structured_value(
    class_code: int, epc: int
) -> None:
    """Every curated CollectionBinding's result_epc must have a value tree."""
    assert epc in REGISTRY.structured_values.get(class_code, {})


def _all_value_definitions(
    value_def: PropertyValueDefinition,
) -> Iterator[PropertyValueDefinition]:
    from pyhems.definitions import ArrayDefinition, ObjectDefinition, OneOfDefinition

    yield value_def
    if isinstance(value_def, ObjectDefinition):
        for field in value_def.fields:
            yield from _all_value_definitions(field.value)
    elif isinstance(value_def, ArrayDefinition):
        yield from _all_value_definitions(value_def.item)
    elif isinstance(value_def, OneOfDefinition):
        for option in value_def.options:
            yield from _all_value_definitions(option)


def test_all_structured_values_are_byte_size_consistent() -> None:
    """Every 0x0287 structured value tree decodes without a byte-size mismatch.

    Regression guard: OneOfDefinition options must agree on byte size, and
    Scalar leaves must have a positive declared size.

    Scoped to class 0x0287 (this feature's actual scope; see
    docs/ha-0287-epc-be-implementation-report-v2.md) rather than every MRA
    class: a handful of unrelated properties elsewhere (e.g. 0x027C EPC
    0xD1, 0x02A7 EPC 0xD7) use an MRA ``oneOf`` between a full nested
    schedule object and an opaque sentinel of a different total byte
    width — a shape the current fixed-width field model does not resolve.
    Nothing in this change set decodes those properties, so this is a
    known, pre-existing limitation of the generic layer rather than a
    regression.
    """
    from pyhems.codecs import value_definition_byte_size
    from pyhems.definitions import ArrayDefinition, OneOfDefinition, ScalarDefinition

    for epc, value_def in REGISTRY.structured_values[0x0287].items():
        for node in _all_value_definitions(value_def):
            if isinstance(node, ScalarDefinition):
                assert node.size > 0, (
                    f"class 0x0287 EPC 0x{epc:02X}: ScalarDefinition.size must be > 0"
                )
            elif isinstance(node, OneOfDefinition):
                # Raises ValueError if options disagree on byte size.
                value_definition_byte_size(node)
            elif isinstance(node, ArrayDefinition):
                assert node.item_size > 0, (
                    f"class 0x0287 EPC 0x{epc:02X}: ArrayDefinition.item_size must be > 0"
                )


if __name__ == "__main__":
    pytest.main([__file__])
