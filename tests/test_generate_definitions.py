"""Tests for generated definitions.

Ensures generated entities expose valid ``get``/``set`` access strings and
unique enum value labels.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from pyhems import REGISTRY, EntityDefinition

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


if __name__ == "__main__":
    pytest.main([__file__])
