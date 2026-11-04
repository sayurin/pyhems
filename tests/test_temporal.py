"""Temporal values preserve their meaning and declared wire precision."""

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from pyhems import (
    REGISTRY,
    TemporalCodec,
    TemporalDefinition,
    TemporalKind,
    get_codec_for_epc,
)


@pytest.mark.parametrize(
    ("class_code", "epc", "edt", "expected"),
    [
        (0x0130, 0x97, "0830", time(8, 48)),
        (0x0130, 0x92, "ff3b", timedelta(hours=255, minutes=59)),
        (0x0130, 0x98, "07e8021d", date(2024, 2, 29)),
        (0x02A7, 0xCD, "07ea0b030a2d", datetime(2026, 11, 3, 10, 45)),
        (0x0272, 0xDB, "ffff", "limitless"),
        (0x03D3, 0xDB, "ffff", "unknown"),
    ],
)
def test_scalar_temporal(class_code: int, epc: int, edt: str, expected: object) -> None:
    codec = get_codec_for_epc(class_code, epc)
    assert codec.decode(bytes.fromhex(edt)) == expected


@pytest.mark.parametrize(
    ("kind", "size", "maximum_hour", "value", "edt"),
    [
        (TemporalKind.DATE, 4, 23, date(2024, 2, 29), "07e8021d"),
        (TemporalKind.DATETIME, 6, 23, datetime(2026, 11, 3, 10, 45), "07ea0b030a2d"),
        (
            TemporalKind.DATETIME,
            7,
            23,
            datetime(2026, 11, 3, 10, 45, 59),
            "07ea0b030a2d3b",
        ),
        (TemporalKind.CLOCK, 2, 23, time(23, 59), "173b"),
        (TemporalKind.CLOCK, 3, 23, time(1, 30, 15), "011e0f"),
        (TemporalKind.DURATION, 2, 255, timedelta(hours=255, minutes=59), "ff3b"),
        (TemporalKind.DURATION, 2, 255, timedelta(hours=255), "ff00"),
        (TemporalKind.DURATION, 2, 23, timedelta(), "0000"),
        (
            TemporalKind.DURATION,
            3,
            23,
            timedelta(hours=1, minutes=30, seconds=15),
            "011e0f",
        ),
    ],
)
def test_round_trip(
    kind: TemporalKind,
    size: int,
    maximum_hour: int,
    value: date | time | timedelta,
    edt: str,
) -> None:
    codec = TemporalCodec(TemporalDefinition(kind, size, maximum_hour))
    assert codec.encode(value) == bytes.fromhex(edt)
    assert codec.decode(bytes.fromhex(edt)) == value
    assert codec.decode(bytes.fromhex(edt) + b"\x00") is None
    assert codec.decode(bytes.fromhex(edt)[:-1]) is None


@pytest.mark.parametrize(
    ("kind", "size", "expected"),
    [
        (TemporalKind.DATE, 4, timedelta(days=1)),
        (TemporalKind.CLOCK, 2, timedelta(minutes=1)),
        (TemporalKind.CLOCK, 3, timedelta(seconds=1)),
        (TemporalKind.DURATION, 2, timedelta(minutes=1)),
        (TemporalKind.DURATION, 3, timedelta(seconds=1)),
        (TemporalKind.DATETIME, 6, timedelta(minutes=1)),
        (TemporalKind.DATETIME, 7, timedelta(seconds=1)),
    ],
)
def test_temporal_resolution(
    kind: TemporalKind, size: int, expected: timedelta
) -> None:
    assert TemporalCodec(TemporalDefinition(kind, size)).resolution == expected


@pytest.mark.parametrize("size", [2, 3])
@pytest.mark.parametrize("maximum_hour", [0, 23, 255])
def test_duration_maximum(size: int, maximum_hour: int) -> None:
    codec = TemporalCodec(TemporalDefinition(TemporalKind.DURATION, size, maximum_hour))
    expected = timedelta(hours=maximum_hour, minutes=59)
    if size == 3:
        expected += timedelta(seconds=59)
    assert codec.maximum_duration == expected
    assert codec.decode(codec.encode(codec.maximum_duration)) == expected
    with pytest.raises(ValueError, match="range"):
        codec.encode(codec.maximum_duration + codec.resolution)


@pytest.mark.parametrize(
    ("kind", "size"),
    [(TemporalKind.DATE, 4), (TemporalKind.CLOCK, 2), (TemporalKind.DATETIME, 6)],
)
def test_maximum_duration_requires_duration(kind: TemporalKind, size: int) -> None:
    with pytest.raises(TypeError, match="duration"):
        _ = TemporalCodec(TemporalDefinition(kind, size)).maximum_duration


@pytest.mark.parametrize(
    ("epc", "value"),
    [
        (0x98, datetime(2024, 2, 29)),
        (0x97, time(1, 2, 3)),
        (0x97, time(1, 2, microsecond=1)),
        (0x97, time(1, 2, tzinfo=UTC)),
        (0x97, time(1, 2, fold=1)),
        (0x97, timedelta(minutes=1)),
        (0x92, time(1, 2)),
        (0x92, timedelta(seconds=-1)),
        (0x92, timedelta(seconds=1)),
        (0x92, timedelta(microseconds=1)),
        (0x92, timedelta(hours=256)),
    ],
)
def test_reject_unrepresentable_values(
    epc: int, value: date | time | timedelta
) -> None:
    with pytest.raises(ValueError, match=r"type|Seconds|Expected|range"):
        get_codec_for_epc(0x0130, epc).encode(value)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 1, second=1),
        datetime(2026, 1, 1, microsecond=1),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, fold=1),
        date(2026, 1, 1),
    ],
)
def test_datetime_rejects_loss(value: date) -> None:
    with pytest.raises(ValueError, match=r"type|Seconds|Expected"):
        get_codec_for_epc(0x02A7, 0xCD).encode(value)


@pytest.mark.parametrize(
    ("class_code", "epc", "edt"),
    [
        (0x0130, 0x97, "1800"),
        (0x0130, 0x97, "003c"),
        (0x0130, 0x98, "07e9021d"),
        (0x02A7, 0xCD, "000001010000"),
    ],
)
def test_invalid_and_unsupported_temporal(
    class_code: int, epc: int, edt: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG", logger="pyhems.codecs"):
        assert get_codec_for_epc(class_code, epc).decode(bytes.fromhex(edt)) is None
    assert caplog.records


def test_special_states_encoding() -> None:
    assert get_codec_for_epc(0x0272, 0xDA).encode("limitless") == b"\xff\xff"
    assert get_codec_for_epc(0x03D3, 0xDB).encode("unknown") == b"\xff\xff"


@pytest.mark.parametrize("epc", [0xDB, 0xDC, 0xED])
def test_readonly_duration_codec_does_not_enforce_access(epc: int) -> None:
    """Like existing codecs, conversion does not enforce EPC access rights."""
    codec = get_codec_for_epc(0x03D3, epc)
    assert codec.encode("unknown") == b"\xff\xff"
    assert codec.encode(timedelta(minutes=1)) == b"\x00\x01"


def test_generator_rejects_duplicate_and_invalid_classifications() -> None:
    generator = import_module("scripts.generate_definitions")
    entry = {"class_code": 0x0130, "epc": 0x91, "kind": "clock"}
    with pytest.raises(ValueError, match="duplicate"):
        generator._load_temporal_kinds({"temporal_kinds": [entry, entry]})
    with pytest.raises(ValueError, match="duplicate"):
        generator._load_temporal_kinds({"temporal_kinds": [{**entry, "kind": "date"}]})


def test_generator_rejects_unused_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = import_module("scripts.generate_definitions")
    custom = generator._load_custom_definitions(generator.CUSTOM_DEFINITIONS_FILE)
    custom["temporal_kinds"].append(
        {"class_code": 0xFFFF, "epc": 0x91, "kind": "clock"}
    )
    monkeypatch.setattr(generator, "_load_custom_definitions", lambda _: custom)
    with pytest.raises(ValueError, match="Unused temporal"):
        generator.generate_definitions(Path("mra"))


def test_scalar_temporal_inventory() -> None:
    definitions = {
        entity.id: entity
        for entities in REGISTRY.entities.values()
        for entity in entities
        if entity.temporal is not None
    }
    assert len(definitions) == 50
    assert Counter(
        temporal.kind
        for entity in definitions.values()
        if (temporal := entity.temporal) is not None
    ) == {
        TemporalKind.CLOCK: 21,
        TemporalKind.DURATION: 19,
        TemporalKind.DATE: 8,
        TemporalKind.DATETIME: 2,
    }


@pytest.mark.parametrize("epc", [0xE1, 0xE5, 0xE9])
def test_generator_excludes_complex_temporal_oneof(epc: int) -> None:
    """A temporal oneOf with non-state alternatives is excluded as a whole."""
    assert not any(entity.epc == epc for entity in REGISTRY.entities[0x03D3])


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "time"},
        {"type": "date", "size": 2},
        {"type": "date-time", "size": 4},
    ],
)
def test_generator_rejects_invalid_temporal(spec: dict[str, Any]) -> None:
    generator = import_module("scripts.generate_definitions")
    _, definitions = generator._load_mra_metadata(Path("mra"))
    prop = generator._parse_mra_property(
        {
            "epc": "0x97",
            "propertyName": {"en": "Time", "ja": "Time"},
            "descriptions": {"en": "Time", "ja": "Time"},
            "accessRule": {"get": "required", "set": "optional", "inf": "optional"},
            "data": spec,
        },
        definitions,
    )
    with pytest.raises(ValueError, match=r"Unclassified|Unsupported"):
        generator._build_temporal_entity(0x0130, prop, spec, definitions, {}, set())


@pytest.mark.parametrize(
    "spec",
    [
        {"oneOf": [{"type": "date"}, {"type": "number", "format": "uint32"}]},
        {
            "oneOf": [
                {"type": "date"},
                {"type": "level", "base": "0xA000", "maximum": 60},
            ]
        },
        {"oneOf": [{"type": "date"}, {"type": "object", "properties": {}}]},
    ],
)
def test_generator_skips_complex_temporal_oneof(spec: dict[str, Any]) -> None:
    generator = import_module("scripts.generate_definitions")
    _, definitions = generator._load_mra_metadata(Path("mra"))
    prop = generator._parse_mra_property(
        {
            "epc": "0x97",
            "propertyName": {"en": "Time", "ja": "Time"},
            "descriptions": {"en": "Time", "ja": "Time"},
            "accessRule": {"get": "required", "set": "optional", "inf": "optional"},
            "data": spec,
        },
        definitions,
    )
    assert (
        generator._build_temporal_entity(
            0x0130, prop, spec, definitions, {(0x0130, 0x97): TemporalKind.CLOCK}, set()
        )
        is None
    )


@pytest.mark.parametrize(
    ("class_code", "epc"), [(0x02A6, 0xB9), (0x02A7, 0xCE), (0x027C, 0xD1)]
)
def test_generator_does_not_expose_structured_temporal(
    class_code: int, epc: int
) -> None:
    """Existing structured properties do not become scalar temporal entities."""
    assert not any(entity.epc == epc for entity in REGISTRY.entities[class_code])
