"""Ceiling fan class 0x013A catalog, codecs, and verified control frames."""

import pytest

from pyhems import (
    CONTROLLER_INSTANCE,
    EOJ,
    ESV,
    REGISTRY,
    DeviceClass,
    Frame,
    Property,
    ceiling_fan_set_properties,
    get_codec_for_epc,
)

_CLASS = DeviceClass.CEILING_FAN
_CLASS_EPCS = frozenset(
    {
        0x93,
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
        0xFE,
    }
)
_FAN = EOJ(0x013A01)
_SILENT_30 = "1081717105ff01013a016103800130fc0131f00133"


def _entities():
    return {entity.epc: entity for entity in REGISTRY.entities[_CLASS]}


def _set_frame(tid: int, **kwargs: object) -> bytes:
    return Frame(
        tid=tid,
        seoj=CONTROLLER_INSTANCE,
        deoj=_FAN,
        esv=ESV.SETC,
        properties=ceiling_fan_set_properties(**kwargs),
    ).encode()


def _payload(**kwargs: object) -> bytes:
    return b"".join(
        bytes([prop.epc, prop.pdc]) + prop.edt
        for prop in ceiling_fan_set_properties(**kwargs)
    )


def test_ceiling_fan_is_a_device_class() -> None:
    """Class 0x013A is the ceiling fan, with the appendix names."""
    device = REGISTRY.devices[_CLASS]
    assert _CLASS == 0x013A
    assert device.class_code == 0x013A
    assert device.name_en == "Ceiling fan"
    assert device.name_ja == "シーリングファン"


def test_class_properties_are_optional_and_unscoped() -> None:
    """Fan and light properties are optional, so a unit may omit the light."""
    specific = [
        entity
        for entity in REGISTRY.entities[_CLASS]
        if entity.id.startswith("class_013a_")
    ]
    assert {entity.epc for entity in specific} == _CLASS_EPCS
    assert all(entity.manufacturer_code is None for entity in specific)
    assert all(
        entity.get == "optional" and entity.set == "optional" for entity in specific
    )


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
        (0xFD, b"\x03", "wifi"),
        (0xFC, b"\x30", True),
        (0xFE, b"\x40", "none"),
        (0xFE, b"\x41", "melody_1"),
        (0xFE, b"\x00", None),
        (0x93, b"\x42", "publicNetwork"),
        (0x93, b"\x41", "notPublicNetwork"),
    ],
)
def test_property_codecs(epc: int, edt: bytes, expected: object) -> None:
    """Each catalog value decodes to the key or number the fan returns."""
    codec = get_codec_for_epc(_CLASS, epc)
    assert codec.decode(edt) == expected
    if expected is not None:
        assert codec.encode(expected) == edt


def test_silent_speed_and_power_on_frame() -> None:
    """Silent 30% and power on at 30% are the same verified SetC."""
    assert _set_frame(0x7171, power=True, speed="level_3").hex() == _SILENT_30


def test_beeping_speed_frame_changes_only_the_buzzer() -> None:
    """0xFC = 0x30 is the confirmation beep. The rest of the frame is unchanged."""
    silent = _set_frame(0x7171, power=True, speed="level_3")
    beeping = _set_frame(0x7171, power=True, silent=False, speed="level_3")
    assert beeping.hex() == "1081717105ff01013a016103800130fc0130f00133"
    assert silent.replace(bytes.fromhex("fc0131"), bytes.fromhex("fc0130")) == beeping


def test_silent_power_off_frame() -> None:
    """Power off is operation status and a silent buzzer, with no speed byte."""
    assert (
        _set_frame(0x8181, power=False).hex() == "1081818105ff01013a016102800131fc0131"
    )


def test_power_on_without_a_change_uses_the_stored_speed() -> None:
    """Power on with no further property turns the fan on at its stored speed."""
    assert _payload(power=True) == bytes.fromhex("800130fc0131")


@pytest.mark.parametrize(
    ("kwargs", "payload"),
    [
        ({"air_flow_direction": "up"}, "800130fc0131f10142"),
        ({"air_flow_direction": "down"}, "800130fc0131f10141"),
        ({"natural_wind": True}, "800130fc0131f20130"),
        ({"natural_wind": False}, "800130fc0131f20131"),
    ],
)
def test_direction_and_natural_wind_frames(
    kwargs: dict[str, object], payload: str
) -> None:
    """Direction and natural wind use power, a silent buzzer, and the property."""
    assert _payload(power=True, **kwargs) == bytes.fromhex(payload)


def test_light_on_includes_source_mode_and_no_melody() -> None:
    """Light on is not applied by power and 0xF3 alone."""
    assert _payload(power=True, light=True) == bytes.fromhex(
        "800130fd0103fc0131fe0140f30130f40142"
    )
    assert _payload(power=True, light=True, brightness=11) == bytes.fromhex(
        "800130fd0103fc0131fe0140f30130f40142f5010b"
    )


def test_light_off_includes_source_and_no_melody() -> None:
    """Light off still marks the write as coming from Wi-Fi."""
    assert _payload(power=True, light=False) == bytes.fromhex(
        "800130fd0103fc0131fe0140f30131"
    )


def test_power_off_rejects_other_properties() -> None:
    """A speed byte does not belong on a power-off write."""
    with pytest.raises(ValueError, match="Power off"):
        ceiling_fan_set_properties(power=False, speed="level_3")


def test_unknown_speed_key_is_rejected() -> None:
    """An undefined speed step is not sent."""
    with pytest.raises(ValueError, match="Unknown enum key"):
        ceiling_fan_set_properties(power=True, speed="level_11")


def test_fan_only_writes_omit_source_and_melody() -> None:
    """Speed, direction, and natural wind do not need the Wi-Fi prefix."""
    properties = ceiling_fan_set_properties(
        power=True,
        speed="level_3",
        air_flow_direction="down",
        natural_wind=True,
    )
    assert [prop.epc for prop in properties] == [0x80, 0xFC, 0xF0, 0xF1, 0xF2]


def test_light_write_property_order() -> None:
    """A combined light write carries the Wi-Fi source and no melody."""
    properties = ceiling_fan_set_properties(
        power=True,
        speed="level_3",
        air_flow_direction="down",
        natural_wind=True,
        light=True,
        light_mode="night",
        brightness=11,
        color=1,
        night_lighting="medium",
    )
    assert [prop.epc for prop in properties] == [
        0x80,
        0xFD,
        0xFC,
        0xFE,
        0xF0,
        0xF1,
        0xF2,
        0xF3,
        0xF4,
        0xF5,
        0xF6,
        0xF7,
    ]
    assert properties[1].edt == b"\x03"
    assert properties[3].edt == b"\x40"
    assert properties[-1].edt == b"\x32"


def test_read_power_speed_and_buzzer_frame() -> None:
    """A Get of power, speed, and the buzzer matches the probed read frame."""
    frame = Frame(
        tid=0x6161,
        seoj=CONTROLLER_INSTANCE,
        deoj=_FAN,
        esv=ESV.GET,
        properties=[Property(epc=0x80), Property(epc=0xF0), Property(epc=0xFC)],
    )
    assert frame.encode().hex() == "1081616105ff01013a0162038000f000fc00"
