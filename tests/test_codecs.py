"""Tests for :mod:`pyhems.codecs`."""

from __future__ import annotations

import pytest

from pyhems import (
    BinaryCodec,
    EntityDefinition,
    EnumCodec,
    EnumValue,
    InstallationLocation,
    InstallationLocationCodec,
    NumericCodec,
    get_codec,
    get_codec_for_epc,
    load_definitions_registry,
)


def _make_entity(
    *,
    epc: int = 0x80,
    mra_format: str | None = None,
    enum_values: tuple[EnumValue, ...] = (),
    minimum: float | None = None,
    maximum: float | None = None,
    multiple_of: float = 1.0,
    byte_offset: int = 0,
) -> EntityDefinition:
    """Build a minimal :class:`EntityDefinition` for codec tests."""
    return EntityDefinition(
        id=f"class_0000_epc_{epc:02x}",
        epc=epc,
        name_en="test",
        name_ja="test",
        get="required",
        set="optional",
        format=mra_format,
        enum_values=enum_values,
        minimum=minimum,
        maximum=maximum,
        multiple_of=multiple_of,
        byte_offset=byte_offset,
    )


class TestBinaryCodec:
    """Tests for :class:`BinaryCodec` selection and round-trip."""

    def test_get_codec_returns_binary_for_true_false_enum(self) -> None:
        """``true``/``false`` enum keys select :class:`BinaryCodec`."""
        entity = _make_entity(
            enum_values=(
                EnumValue(edt=0x30, key="true", name_en="On", name_ja="入"),
                EnumValue(edt=0x31, key="false", name_en="Off", name_ja="切"),
            )
        )
        codec = get_codec(entity)
        assert isinstance(codec, BinaryCodec)
        assert codec.on_edt == 0x30
        assert codec.off_edt == 0x31

    def test_encode_decode_round_trip(self) -> None:
        """``encode``/``decode`` are inverses for valid bool input."""
        codec = BinaryCodec(on_edt=0x30, off_edt=0x31)
        assert codec.encode(True) == b"\x30"
        assert codec.encode(False) == b"\x31"
        assert codec.decode(b"\x30") is True
        assert codec.decode(b"\x31") is False

    def test_decode_returns_none_for_empty_or_unknown(self) -> None:
        """Empty or unmapped EDT decodes to ``None``."""
        codec = BinaryCodec(on_edt=0x30, off_edt=0x31)
        assert codec.decode(b"") is None
        assert codec.decode(b"\x42") is None


class TestEnumCodec:
    """Tests for :class:`EnumCodec` selection and round-trip."""

    def test_get_codec_returns_enum_for_multi_state(self) -> None:
        """Multi-state ``enum_values`` (not binary) selects :class:`EnumCodec`."""
        entity = _make_entity(
            enum_values=(
                EnumValue(edt=0x41, key="auto", name_en="Auto", name_ja="自動"),
                EnumValue(edt=0x42, key="cool", name_en="Cool", name_ja="冷房"),
                EnumValue(edt=0x43, key="heat", name_en="Heat", name_ja="暖房"),
            )
        )
        codec = get_codec(entity)
        assert isinstance(codec, EnumCodec)
        assert codec.encode("cool") == b"\x42"
        assert codec.decode(b"\x43") == "heat"

    def test_encode_unknown_key_raises(self) -> None:
        """``encode`` raises :class:`ValueError` for keys not in ``enum_values``."""
        codec = EnumCodec(by_key={"a": 0x10}, by_edt={0x10: "a"})
        with pytest.raises(ValueError, match="Unknown enum key"):
            codec.encode("missing")

    def test_decode_empty_returns_none(self) -> None:
        """Empty EDT decodes to ``None``."""
        codec = EnumCodec(by_key={"a": 0x10}, by_edt={0x10: "a"})
        assert codec.decode(b"") is None
        assert codec.decode(b"\x99") is None


class TestNumericCodec:
    """Tests for :class:`NumericCodec` selection and round-trip."""

    def test_get_codec_returns_numeric_for_format(self) -> None:
        """A populated ``format`` field selects :class:`NumericCodec`."""
        entity = _make_entity(mra_format="uint8", minimum=0, maximum=100)
        codec = get_codec(entity)
        assert isinstance(codec, NumericCodec)
        assert codec.encode(42) == b"\x2a"
        assert codec.decode(b"\x2a") == 42

    def test_scaled_round_trip(self) -> None:
        """``multiple_of`` (scale) is applied symmetrically."""
        codec = NumericCodec(
            mra_format="uint8",
            scale=0.1,
            minimum=0,
            maximum=1000,
            byte_offset=0,
        )
        assert codec.encode(2.5) == b"\x19"
        assert codec.decode(b"\x19") == pytest.approx(2.5)

    def test_encode_with_byte_offset_rejected(self) -> None:
        """Encoding into a packed EDT (``byte_offset`` > 0) is not supported."""
        codec = NumericCodec(
            mra_format="uint8",
            scale=1.0,
            minimum=None,
            maximum=None,
            byte_offset=1,
        )
        with pytest.raises(ValueError, match="byte_offset"):
            codec.encode(1)

    def test_decode_out_of_range_returns_none(self) -> None:
        """Values outside [minimum, maximum] decode to ``None``."""
        codec = NumericCodec(
            mra_format="uint8",
            scale=1.0,
            minimum=0,
            maximum=100,
            byte_offset=0,
        )
        assert codec.decode(b"\xff") is None


class TestInstallationLocationCodec:
    """Tests for :class:`InstallationLocationCodec`."""

    def test_round_trip_living_room_instance_1(self) -> None:
        """Living room (LLLL=0x1, NNN=1) round-trips through one byte."""
        codec = InstallationLocationCodec()
        original = InstallationLocation(
            code=0x1,
            key="living_room",
            name="Living room",
            name_ja="リビング",
            instance=1,
        )
        encoded = codec.encode(original)
        assert encoded == b"\x09"
        decoded = codec.decode(encoded)
        assert decoded is not None
        assert decoded.code == 0x1
        assert decoded.instance == 1
        assert decoded.key == "living_room"

    def test_encode_rejects_unknown_code(self) -> None:
        """Codes outside the standard table raise :class:`ValueError`."""
        codec = InstallationLocationCodec()
        bad = InstallationLocation(code=0x0, key="x", name="x", name_ja="x", instance=0)
        with pytest.raises(ValueError, match="Unknown installation location"):
            codec.encode(bad)

    def test_encode_rejects_instance_out_of_range(self) -> None:
        """Instances outside 0..7 raise :class:`ValueError`."""
        codec = InstallationLocationCodec()
        bad = InstallationLocation(
            code=0x1,
            key="living_room",
            name="Living room",
            name_ja="リビング",
            instance=8,
        )
        with pytest.raises(ValueError, match="instance must be"):
            codec.encode(bad)


def test_get_codec_raises_when_no_format_and_no_enum() -> None:
    """An entity with neither ``format`` nor ``enum_values`` is rejected."""
    # _make_entity asserts the underlying invariant; build directly to test
    # the codec selector.
    entity = EntityDefinition(
        id="x",
        epc=0x99,
        name_en="x",
        name_ja="x",
        get="required",
        set="optional",
        format=None,
        enum_values=(),
    )
    with pytest.raises(ValueError, match="Cannot determine codec"):
        get_codec(entity)


# ============================================================================
# get_codec_for_epc — binary codec definitions-level guarantees
# ============================================================================

_DEFINITIONS = load_definitions_registry()


def test_get_codec_for_epc_raises_for_unknown_epc() -> None:
    """get_codec_for_epc raises LookupError when EPC is not in the class."""
    with pytest.raises(LookupError, match="0xFF not found"):
        get_codec_for_epc(_DEFINITIONS, 0x0130, 0xFF)


# Class codes that use EPC 0x80 (Operation Status) as a binary on/off property.
# These correspond to the dedicated-platform device classes in the HA integration.
_OP_STATUS_CLASS_CODES = [
    0x0130,  # Home air conditioner
    0x0133,  # Ventilation fan
    0x0134,  # Air conditioner / ventilation fan
    0x0135,  # Air cleaner
    0x026B,  # Electric water heater
    0x0290,  # General lighting
    0x0291,  # Mono-functional lighting
    0x02A3,  # Lighting system
    0x02A4,  # Extended lighting system
]


@pytest.mark.parametrize("class_code", _OP_STATUS_CLASS_CODES)
def test_op_status_epc_yields_binary_codec(class_code: int) -> None:
    """EPC 0x80 (Operation Status) must yield a BinaryCodec for every dedicated-platform class.

    This guarantees the HA integration can always obtain the codec at entity
    init time and does not need a hardcoded fallback.
    """
    codec = get_codec_for_epc(_DEFINITIONS, class_code, 0x80)
    assert isinstance(codec, BinaryCodec), (
        f"Expected BinaryCodec for EPC 0x80 on class 0x{class_code:04X}, got {codec!r}"
    )


def test_lock_setting_epc_yields_binary_codec() -> None:
    """EPC 0xE0 (Lock Setting 1) must yield a BinaryCodec for class 0x026F (Electric Lock).

    This guarantees the HA integration can always obtain the codec at entity
    init time and does not need a hardcoded fallback.
    """
    codec = get_codec_for_epc(_DEFINITIONS, 0x026F, 0xE0)
    assert isinstance(codec, BinaryCodec), (
        f"Expected BinaryCodec for EPC 0xE0 on class 0x026F (Electric Lock), got {codec!r}"
    )


def test_enum_codec_no_key_collisions_in_definitions() -> None:
    """No two enum_values entries share the same EDT byte with different keys.

    An EDT collision would make ``decode()`` ambiguous (the same EDT byte maps
    to two different keys), so the round-trip ``encode(k) -> decode()`` would
    not always return ``k``.  This test confirms the MRA definitions are free
    of such collisions.

    Note: *key* collisions (same key → different EDT bytes) are also absent
    from ``definitions.json`` because EPC 0x93 ("Remote control setting"),
    the only MRA property where ``name`` values were reused across distinct
    physical states, is excluded during generation (see ``_EXCLUDED_EPCS`` in
    ``scripts/generate_definitions.py``).  ``get_codec`` and
    ``EnumCodec.from_mapping`` therefore use plain dict comprehensions; the
    absence of collisions is the invariant this test enforces.
    """
    errors: list[str] = []
    for class_code, entities in _DEFINITIONS.entities.items():
        for entity_def in entities:
            if len(entity_def.enum_values) <= 2:
                continue  # BinaryCodec or no enum — not relevant
            edt_to_keys: dict[int, list[str]] = {}
            key_to_edts: dict[str, list[int]] = {}
            for ev in entity_def.enum_values:
                edt_to_keys.setdefault(ev.edt, []).append(ev.key)
                key_to_edts.setdefault(ev.key, []).append(ev.edt)
            for edt_byte, keys in edt_to_keys.items():
                if len(set(keys)) > 1:  # pragma: no cover
                    errors.append(
                        f"class 0x{class_code:04X} EPC 0x{entity_def.epc:02X}: "
                        f"EDT 0x{edt_byte:02X} maps to multiple keys: {keys}"
                    )
            for key, edts in key_to_edts.items():
                if len(set(edts)) > 1:  # pragma: no cover
                    errors.append(
                        f"class 0x{class_code:04X} EPC 0x{entity_def.epc:02X}: "
                        f"key {key!r} maps to multiple EDT bytes: "
                        f"{[f'0x{e:02X}' for e in edts]}"
                    )
    assert not errors, "\n".join(errors)


def test_get_codec_succeeds_for_all_entity_definitions() -> None:
    """get_codec must succeed for every entity definition that has codec-able data.

    This guarantees that the HA integration can always call get_codec(entity_def)
    for any definition with enum_values or a format field, without needing
    manual NumericCodec/EnumCodec construction as a fallback.
    """
    errors: list[str] = []
    for class_code, entities in _DEFINITIONS.entities.items():
        for entity_def in entities:
            if not entity_def.enum_values and entity_def.format is None:
                continue
            try:
                codec = get_codec(entity_def)
            except ValueError as exc:  # pragma: no cover
                errors.append(
                    f"class 0x{class_code:04X} EPC 0x{entity_def.epc:02X}: {exc}"
                )
            else:
                if not isinstance(
                    codec, (BinaryCodec, EnumCodec, NumericCodec)
                ):  # pragma: no cover
                    errors.append(
                        f"class 0x{class_code:04X} EPC 0x{entity_def.epc:02X}: "
                        f"unexpected codec type {type(codec)}"
                    )
    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Platform-specific EPC guarantees
# These tests assert the exact codec type that HA platforms depend on,
# so that a pyhems definition change is caught before it breaks the integration.
# ---------------------------------------------------------------------------

# (class_code, epc, expected_type, description)
_PLATFORM_EPC_GUARANTEES: list[tuple[int, int, type, str]] = [
    # climate - Home Air Conditioner (0x0130)
    (0x0130, 0xB3, NumericCodec, "target temperature"),
    (0x0130, 0xBB, NumericCodec, "room temperature"),
    (0x0130, 0xBA, NumericCodec, "room humidity"),
    # water_heater - Electric Water Heater (0x026B)
    (0x026B, 0xB3, NumericCodec, "target temperature"),
    (0x026B, 0xC1, NumericCodec, "measured water temperature"),
]


@pytest.mark.parametrize(
    ("class_code", "epc", "expected_type", "description"),
    _PLATFORM_EPC_GUARANTEES,
    ids=[
        f"0x{cc:04X}_0x{epc:02X}_{desc}"
        for cc, epc, _, desc in _PLATFORM_EPC_GUARANTEES
    ],
)
def test_platform_epc_codec_type(
    class_code: int,
    epc: int,
    expected_type: type,
    description: str,
) -> None:
    """Assert codec type for EPCs that HA platforms rely on being non-None.

    If any of these assertions fails, a pyhems definition change has broken
    a guarantee that the HA integration depends on.
    """
    codec = get_codec_for_epc(_DEFINITIONS, class_code, epc)
    assert isinstance(codec, expected_type), (
        f"class 0x{class_code:04X} EPC 0x{epc:02X} ({description}): "
        f"expected {expected_type.__name__}, got {type(codec).__name__}"
    )
