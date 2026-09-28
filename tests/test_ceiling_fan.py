"""Ceiling fan class 0x013A catalog and Panasonic-scoped codecs."""

import pytest

from pyhems import (
    REGISTRY,
    DeviceClass,
    get_codec_for_epc,
)

_CLASS = DeviceClass.CEILING_FAN
_CLASS_EPCS = frozenset(
    {
        0xF0,
        0xF1,
        0xF2,
        0xF3,
        0xF4,
        0xF5,
        0xF6,
        0xF7,
        0xFC,
        0xFD,
    }
)
_PANASONIC = 0x0000FE


def _entities():
    return {entity.epc: entity for entity in REGISTRY.entities[_CLASS]}


def test_ceiling_fan_is_a_device_class() -> None:
    """Class 0x013A is the ceiling fan, with the appendix names."""
    device = REGISTRY.devices[_CLASS]
    assert _CLASS == 0x013A
    assert device.class_code == 0x013A
    assert device.name_en == "Ceiling fan"
    assert device.name_ja == "シーリングファン"


def test_class_properties_are_panasonic_scoped() -> None:
    """Fan and light EPCs are optional Panasonic maker-specific properties."""
    specific = [
        entity
        for entity in REGISTRY.entities[_CLASS]
        if entity.id.startswith("class_013a_")
    ]
    assert {entity.epc for entity in specific} == _CLASS_EPCS
    assert all(entity.manufacturer_code == _PANASONIC for entity in specific)
    assert all(entity.get == "optional" for entity in specific)
    assert all(entity.set == "optional" for entity in specific)


def test_common_power_and_fault_codecs() -> None:
    """Operation status and fault status come from the shared device properties."""
    entities = _entities()
    assert entities[0x80].id == "class_0000_epc_80"
    assert entities[0x88].id == "class_0000_epc_88"

    power = get_codec_for_epc(_CLASS, 0x80)
    assert power.decode(b"\x30") is True
    assert power.decode(b"\x31") is False
    assert power.encode(True) == b"\x30"
    assert power.encode(False) == b"\x31"

    fault = get_codec_for_epc(_CLASS, 0x88)
    assert fault.decode(b"\x41") is True
    assert fault.decode(b"\x42") is False


def test_fan_speed_levels() -> None:
    """Speed steps are level keys. 0x33 is level 3, displayed as 30%."""
    entity = _entities()[0xF0]
    names = {value.key: value.name_en for value in entity.enum_values}
    codec = get_codec_for_epc(_CLASS, 0xF0)

    assert codec.decode(b"\x33") == "level_3"
    assert names["level_3"] == "30%"
    assert codec.encode("level_3") == b"\x33"
    assert codec.decode(b"\x31") == "level_1"
    assert names["level_1"] == "10%"
    assert codec.decode(b"\x3a") == "level_10"
    assert names["level_10"] == "100%"
    assert codec.decode(b"\x30") is None
    assert codec.decode(b"\x3b") is None


@pytest.mark.parametrize(
    ("epc", "edt", "expected"),
    [
        (0xF1, b"\x42", "up"),
        (0xF1, b"\x41", "down"),
        (0xF2, b"\x31", False),
        (0xF2, b"\x30", True),
        (0xF3, b"\x31", False),
        (0xF3, b"\x30", True),
        (0xF4, b"\x42", "normal"),
        (0xF4, b"\x43", "night"),
        (0xF5, b"\x0b", 11),
        (0xF5, b"\x00", None),
        (0xF5, b"\x65", None),
        (0xF6, b"\x00", 0),
        (0xF6, b"\x64", 100),
        (0xF6, b"\x65", None),
        (0xF7, b"\x01", "low"),
        (0xF7, b"\x32", "medium"),
        (0xF7, b"\x64", "high"),
        (0xF7, b"\x02", None),
        (0xF7, b"\x03", None),
        (0xFC, b"\x31", False),
        (0xFD, b"\x01", "remoteController"),
        (0xFD, b"\x03", "wifi"),
        (0xFC, b"\x30", True),
    ],
)
def test_property_codecs(epc: int, edt: bytes, expected: object) -> None:
    """Each catalog value decodes to the key or number the fan returns."""
    codec = get_codec_for_epc(_CLASS, epc)
    assert codec.decode(edt) == expected
    if expected is not None:
        assert codec.encode(expected) == edt
