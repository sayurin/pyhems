"""Tests for :mod:`pyhems.codecs`."""

from __future__ import annotations

import datetime
import struct

import pytest

from pyhems import (
    REGISTRY,
    ArrayDefinition,
    BinaryCodec,
    CollectionBinding,
    EntityDefinition,
    EnumCodec,
    EnumValue,
    InstallationLocation,
    InstallationLocationCodec,
    NumericCodec,
    NumericValueCodec,
    NumericValueEntry,
    ObjectDefinition,
    ObjectField,
    OneOfDefinition,
    ScalarDefinition,
    TimeCodec,
    decode_collection_page,
    decode_property_value,
    get_codec,
    get_codec_for_epc,
    get_collection_binding,
    get_structured_value,
)
from pyhems.codecs import (
    CollectionPage,
    _decode_collection,
    _value_definition_byte_size,
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
        readable=True,
        writable=True,
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
        """datetime.time round-trips through EDT bytes."""
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

    def test_decode_with_byte_offset(self) -> None:
        """byte_offset skips leading EDT bytes."""
        """``byte_offset`` selects the correct byte from a packed EDT."""
        codec = BinaryCodec(on_edt=0x00, off_edt=0x01, byte_offset=1)
        # byte at offset 1 is 0x00 → True (bright)
        assert codec.decode(b"\x62\x00\xc6") is True
        # byte at offset 1 is 0x01 → False (dark)
        assert codec.decode(b"\x62\x01\xc6") is False
        # EDT too short for offset → None
        assert codec.decode(b"\x62") is None

    def test_get_codec_propagates_byte_offset(self) -> None:
        """``get_codec`` passes ``byte_offset`` through to :class:`BinaryCodec`."""
        entity = _make_entity(
            byte_offset=1,
            enum_values=(
                EnumValue(edt=0x00, key="true", name_en="Bright", name_ja="明るい"),
                EnumValue(edt=0x01, key="false", name_en="Dark", name_ja="暗い"),
            ),
        )
        codec = get_codec(entity)
        assert isinstance(codec, BinaryCodec)
        assert codec.byte_offset == 1
        assert codec.decode(b"\x62\x00\xc6") is True


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

    def test_decode_with_byte_offset(self) -> None:
        """``byte_offset`` selects the correct byte from a packed EDT."""
        codec = EnumCodec(
            by_key={"auto": 0x41, "cool": 0x42},
            by_edt={0x41: "auto", 0x42: "cool"},
            byte_offset=2,
        )
        assert codec.decode(b"\x00\x00\x42") == "cool"
        assert codec.decode(b"\x00\x00\x41") == "auto"
        # EDT too short for offset → None
        assert codec.decode(b"\x00\x00") is None

    def test_get_codec_propagates_byte_offset_to_enum(self) -> None:
        """``get_codec`` passes ``byte_offset`` through to :class:`EnumCodec`."""
        entity = _make_entity(
            byte_offset=2,
            enum_values=(
                EnumValue(edt=0x41, key="auto", name_en="Auto", name_ja="自動"),
                EnumValue(edt=0x42, key="cool", name_en="Cool", name_ja="冷房"),
                EnumValue(edt=0x43, key="heat", name_en="Heat", name_ja="暖房"),
            ),
        )
        codec = get_codec(entity)
        assert isinstance(codec, EnumCodec)
        assert codec.byte_offset == 2
        assert codec.decode(b"\x00\x00\x42") == "cool"


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
        """Encoding at a non-zero byte_offset is rejected."""
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

    def test_decode_with_coefficient_resolves_via_context(self) -> None:
        """``coefficients`` multiply by the current value of a sibling EPC.

        Uses class 0x0287 (Power distribution board metering) EPC 0xC0
        (cumulative energy, coefficient EPC 0xC2) from the real registry.
        """
        context = {0xC0: b"\x00\x00\x00\x0a", 0xC2: b"\x02"}  # unit = 0.01
        codec = get_codec_for_epc(0x0287, 0xC0)
        assert isinstance(codec, NumericCodec)
        assert [epc for epc, _ in codec.coefficients] == [0xC2]
        assert codec.decode(context[0xC0], context) == pytest.approx(0.1)

    def test_decode_with_coefficient_without_context_returns_none(self) -> None:
        """A coefficient-bearing codec refuses to guess without ``context``."""
        codec = get_codec_for_epc(0x0287, 0xC0)
        assert codec.decode(b"\x00\x00\x00\x0a") is None

    def test_decode_with_coefficient_missing_sibling_returns_none(self) -> None:
        """``None`` is returned when the coefficient EPC is not yet known."""
        context = {0xC0: b"\x00\x00\x00\x0a"}
        codec = get_codec_for_epc(0x0287, 0xC0)
        assert isinstance(codec, NumericCodec)
        assert codec.decode(context[0xC0], context) is None

    def test_decode_with_undecodable_coefficient_returns_none(self) -> None:
        """An unknown coefficient EDT yields ``None`` instead of a guess."""
        context = {0xC2: b"\x7f"}
        codec = get_codec_for_epc(0x0287, 0xC0)
        assert codec.decode(b"\x00\x00\x00\x0a", context) is None

    def test_encode_with_coefficients_rejected(self) -> None:
        """Encoding a coefficient-bearing property is not supported.

        Resolving the coefficient requires device state, which ``encode()``
        does not receive.
        """
        codec = NumericCodec(
            mra_format="uint32",
            scale=1.0,
            minimum=None,
            maximum=None,
            byte_offset=0,
            coefficients=((0xC2, NumericValueCodec(by_edt={0x00: 1.0})),),
        )
        with pytest.raises(ValueError, match="coefficients"):
            codec.encode(10)

    def test_encode_rejects_value_outside_minimum_maximum(self) -> None:
        """Encoding validates the scaled value against ``minimum``/``maximum``."""
        codec = NumericCodec(
            mra_format="uint8",
            scale=0.5,
            minimum=10,
            maximum=100,
            byte_offset=0,
        )
        assert codec.encode(5) == b"\x0a"
        assert codec.encode(50) == b"\x64"
        with pytest.raises(ValueError, match="out of range"):
            codec.encode(4.5)
        with pytest.raises(ValueError, match="out of range"):
            codec.encode(50.5)

    def test_get_codec_requires_class_code_for_coefficients(self) -> None:
        """Coefficient EPCs cannot be resolved without the device class."""
        entity_def = next(e for e in REGISTRY.entities[0x0287] if e.epc == 0xC0)
        with pytest.raises(ValueError, match="class_code is required"):
            get_codec(entity_def)
        codec = get_codec(entity_def, class_code=0x0287)
        assert isinstance(codec, NumericCodec)
        assert [epc for epc, _ in codec.coefficients] == [0xC2]


class TestNumericValueCodec:
    """Tests for :class:`NumericValueCodec` selection and round-trip."""

    def test_get_codec_returns_numeric_value_codec_for_numeric_values(self) -> None:
        """A populated ``numeric_values`` field selects :class:`NumericValueCodec`."""
        entity = EntityDefinition(
            id="class_0287_epc_c2",
            epc=0xC2,
            name_en="Unit for cumulative amounts of electric energy",
            name_ja="積算電力量単位",
            readable=True,
            writable=False,
            numeric_values=(
                NumericValueEntry(edt=0x00, value=1.0),
                NumericValueEntry(edt=0x01, value=0.1),
                NumericValueEntry(edt=0x02, value=0.01),
            ),
        )
        codec = get_codec(entity)
        assert isinstance(codec, NumericValueCodec)
        assert codec.decode(b"\x00") == 1.0
        assert codec.decode(b"\x01") == 0.1
        assert codec.decode(b"\x02") == 0.01

    def test_decode_returns_none_for_unknown_or_empty_edt(self) -> None:
        """Unmapped or empty EDT decodes to ``None``."""
        codec = NumericValueCodec(by_edt={0x00: 1.0, 0x01: 0.1})
        assert codec.decode(b"\x0a") is None
        assert codec.decode(b"") is None

    def test_encode_round_trip(self) -> None:
        """``encode`` finds the EDT byte matching a known numeric value."""
        codec = NumericValueCodec(by_edt={0x00: 1.0, 0x01: 0.1})
        assert codec.encode(0.1) == b"\x01"

    def test_encode_matches_via_isclose_not_exact_equality(self) -> None:
        """A value that is very close (but not bit-identical) still matches.

        E.g. a value round-tripped through a different computation than the
        one that produced the table entry.
        """
        codec = NumericValueCodec(by_edt={0x03: 0.001, 0x05: 0.00001})
        assert codec.encode(1 / 1000) == b"\x03"
        assert codec.encode(0.001 + 1e-15) == b"\x03"

    def test_encode_unknown_value_raises(self) -> None:
        """Encoding an unmapped value raises :class:`ValueError`."""
        codec = NumericValueCodec(by_edt={0x00: 1.0})
        with pytest.raises(ValueError, match="Unknown numeric value"):
            codec.encode(5.0)

    def test_get_codec_prefers_numeric_value_over_format(self) -> None:
        """``numeric_values`` takes priority even if ``format`` is also set.

        These fields are mutually exclusive by construction, but the
        selection order itself is worth pinning down explicitly.
        """
        entity = EntityDefinition(
            id="x",
            epc=0x99,
            name_en="x",
            name_ja="x",
            readable=True,
            writable=False,
            format="uint8",
            numeric_values=(NumericValueEntry(edt=0x00, value=1.0),),
        )
        assert isinstance(get_codec(entity), NumericValueCodec)


class TestInstallationLocationCodec:
    """Tests for :class:`InstallationLocationCodec`."""

    def test_round_trip_living_room_instance_1(self) -> None:
        """Living room (LLLL=0x1, NNN=1) round-trips through one byte."""
        codec = InstallationLocationCodec()
        original = InstallationLocation.from_code(0x1, instance=1)
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
        bad = InstallationLocation(code=0x0, instance=0)
        with pytest.raises(ValueError, match="Unknown installation location"):
            codec.encode(bad)

    def test_encode_rejects_instance_out_of_range(self) -> None:
        """Instances outside 0..7 raise :class:`ValueError`."""
        codec = InstallationLocationCodec()
        bad = InstallationLocation(code=0x1, instance=8)
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
        readable=True,
        writable=True,
        format=None,
        enum_values=(),
    )
    with pytest.raises(ValueError, match="Cannot determine codec"):
        get_codec(entity)


# ============================================================================
# get_codec_for_epc — binary codec definitions-level guarantees
# ============================================================================

_DEFINITIONS = REGISTRY


def test_get_codec_for_epc_raises_for_unknown_epc() -> None:
    """get_codec_for_epc raises LookupError when EPC is not in the class."""
    with pytest.raises(LookupError, match="0xFF not found"):
        get_codec_for_epc(0x0130, 0xFF)


def test_get_codec_for_epc_is_cached() -> None:
    """Repeat calls for the same (class_code, epc) return the cached codec.

    ``REGISTRY`` is an immutable singleton, so caching avoids repeating the
    linear scan and codec construction on every call (important for
    ``NumericCodec``'s ``coefficient_epcs`` resolution on each ``decode()``).
    """
    assert get_codec_for_epc(0x0287, 0xC0) is get_codec_for_epc(0x0287, 0xC0)


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
    codec = get_codec_for_epc(class_code, 0x80)
    assert isinstance(codec, BinaryCodec), (
        f"Expected BinaryCodec for EPC 0x80 on class 0x{class_code:04X}, got {codec!r}"
    )


def test_lock_setting_epc_yields_binary_codec() -> None:
    """EPC 0xE0 (Lock Setting 1) must yield a BinaryCodec for class 0x026F (Electric Lock).

    This guarantees the HA integration can always obtain the codec at entity
    init time and does not need a hardcoded fallback.
    """
    codec = get_codec_for_epc(0x026F, 0xE0)
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
                codec = get_codec(entity_def, class_code=class_code)
            except ValueError as exc:  # pragma: no cover
                errors.append(
                    f"class 0x{class_code:04X} EPC 0x{entity_def.epc:02X}: {exc}"
                )
            else:
                if not isinstance(
                    codec, (BinaryCodec, EnumCodec, NumericCodec, TimeCodec)
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
    codec = get_codec_for_epc(class_code, epc)
    assert isinstance(codec, expected_type), (
        f"class 0x{class_code:04X} EPC 0x{epc:02X} ({description}): "
        f"expected {expected_type.__name__}, got {type(codec).__name__}"
    )


# ============================================================================
# Generic structured value decoding (PropertyValueDefinition / CollectionBinding)
# ============================================================================


class TestDecodePropertyValueScalar:
    """Tests for :func:`decode_property_value` on :class:`ScalarDefinition`."""

    def test_decodes_number(self) -> None:
        """A plain numeric leaf decodes and applies its scale."""
        value_def = ScalarDefinition(size=2, format="int16", multiple_of=0.1)
        assert decode_property_value(value_def, b"\x00\x0a") == pytest.approx(1.0)

    def test_number_out_of_range_returns_none(self) -> None:
        """A raw value outside [minimum, maximum] decodes to ``None``."""
        value_def = ScalarDefinition(size=1, format="uint8", minimum=1, maximum=252)
        assert decode_property_value(value_def, b"\xfd") is None

    def test_decodes_state_enum(self) -> None:
        """A state leaf decodes the matching multi-byte sentinel EDT to its key."""
        value_def = ScalarDefinition(
            size=4,
            enum_values=(
                EnumValue(edt=0x7FFFFFFE, key="noData", name_en="x", name_ja="x"),
            ),
        )
        assert (
            decode_property_value(value_def, struct.pack(">I", 0x7FFFFFFE)) == "noData"
        )
        assert decode_property_value(value_def, struct.pack(">I", 0)) is None

    def test_decodes_numeric_value_table(self) -> None:
        """A ``numericValue`` leaf decodes via its EDT->float table."""
        value_def = ScalarDefinition(
            size=1, numeric_values=(NumericValueEntry(edt=0x01, value=0.1),)
        )
        assert decode_property_value(value_def, b"\x01") == 0.1

    def test_resolves_coefficient_via_context(self) -> None:
        """``coefficient_epcs`` resolves via ``context`` and ``class_code``."""
        value_def = ScalarDefinition(
            size=4,
            format="uint32",
            minimum=0,
            maximum=99999999,
            coefficient_epcs=(0xC2,),
        )
        context = {0xC2: b"\x02"}  # 0.01
        assert decode_property_value(
            value_def, struct.pack(">I", 10), context, class_code=0x0287
        ) == pytest.approx(0.1)
        # Without the device class the coefficient cannot be resolved.
        assert decode_property_value(value_def, struct.pack(">I", 10), context) is None


class TestDecodePropertyValueOneOf:
    """Tests for :func:`decode_property_value` on :class:`OneOfDefinition`."""

    _CHANNEL = OneOfDefinition(
        options=(
            ScalarDefinition(size=1, format="uint8", minimum=1, maximum=252),
            ScalarDefinition(
                size=1,
                enum_values=(
                    EnumValue(edt=0xFD, key="undefined", name_en="x", name_ja="x"),
                ),
            ),
        )
    )

    def test_first_matching_option_wins(self) -> None:
        """The first option that decodes successfully wins."""
        assert decode_property_value(self._CHANNEL, b"\x05") == 5

    def test_falls_through_to_sentinel_state(self) -> None:
        """A value outside the numeric option's range falls through to the sentinel."""
        assert decode_property_value(self._CHANNEL, b"\xfd") == "undefined"

    def test_no_option_matches_returns_none(self) -> None:
        """``None`` is returned when no option decodes the raw bytes."""
        assert decode_property_value(self._CHANNEL, b"\x00") is None


class TestDecodePropertyValueObject:
    """Tests for :func:`decode_property_value` on :class:`ObjectDefinition`."""

    def test_decodes_sequential_fields(self) -> None:
        """Fixed-width fields decode in order at increasing byte offsets."""
        value_def = ObjectDefinition(
            fields=(
                ObjectField(
                    key="a",
                    name_en="a",
                    name_ja="a",
                    value=ScalarDefinition(size=1, format="uint8"),
                ),
                ObjectField(
                    key="b",
                    name_en="b",
                    name_ja="b",
                    value=ScalarDefinition(size=2, format="uint16"),
                ),
            )
        )
        assert decode_property_value(value_def, b"\x01\x00\x02") == {"a": 1, "b": 2}

    def test_trailing_array_field_consumes_remaining_bytes(self) -> None:
        """A trailing array field consumes the rest of the EDT, not a fixed width."""
        value_def = ObjectDefinition(
            fields=(
                ObjectField(
                    key="start",
                    name_en="start",
                    name_ja="start",
                    value=ScalarDefinition(size=1, format="uint8"),
                ),
                ObjectField(
                    key="items",
                    name_en="items",
                    name_ja="items",
                    value=ArrayDefinition(
                        item=ScalarDefinition(size=2, format="uint16"), item_size=2
                    ),
                ),
            )
        )
        decoded = decode_property_value(value_def, b"\x01\x00\x0a\x00\x0b")
        assert decoded == {"start": 1, "items": (10, 11)}

    def test_backtracks_variable_width_one_of_field(self) -> None:
        """A oneOf field may use the width that leaves a valid suffix."""
        value_def = ObjectDefinition(
            fields=(
                ObjectField(
                    key="start",
                    name_en="start",
                    name_ja="start",
                    value=OneOfDefinition(
                        options=(
                            ScalarDefinition(
                                size=1, format="uint8", minimum=0, maximum=0
                            ),
                            ScalarDefinition(size=2, format="uint16"),
                        )
                    ),
                ),
                ObjectField(
                    key="end",
                    name_en="end",
                    name_ja="end",
                    value=ScalarDefinition(size=1, format="uint8"),
                ),
            )
        )
        assert decode_property_value(value_def, b"\x00\x01\x02") == {
            "start": 1,
            "end": 2,
        }

    def test_decodes_variable_width_top_level_one_of(self) -> None:
        """A top-level oneOf can distinguish a structured value from raw data."""
        value_def = OneOfDefinition(
            options=(
                ObjectDefinition(
                    fields=(
                        ObjectField(
                            key="value",
                            name_en="value",
                            name_ja="value",
                            value=ScalarDefinition(size=2, format="uint16"),
                        ),
                    )
                ),
                ScalarDefinition(size=1),
            )
        )
        assert decode_property_value(value_def, b"\x00\x01") == {"value": 1}
        assert decode_property_value(value_def, b"\xff") == b"\xff"

    def test_decodes_generated_variable_width_one_of_fields(self) -> None:
        """Generated MRA definitions may mix 2-byte sentinels and 3-byte times."""
        value_def = get_structured_value(0x02A7, 0xD7)
        assert value_def is not None
        edt = b"\xff\xff" * 4 + b"\x00\x00" + b"\x00" * 8 + b"AAA"
        assert decode_property_value(value_def, edt) == {
            "chargeStartTime": "invalid",
            "chargeEndTime": "invalid",
            "dischargeStartTime": "invalid",
            "dischargeEndTime": "invalid",
            "dischargeLowerLimit": 0,
            "chargeUpperLimit": 0,
            "peakCutPowerThreshold": 0,
            "peakCutEnergyThreshold": 0,
            "pvSurplusCharging": "true",
            "pvReversePowerFlow": "true",
            "pcsPushUpEffect": "true",
        }

    def test_decodes_generated_variable_width_top_level_one_of(self) -> None:
        """Generated MRA definitions may use a short sentinel for an object."""
        value_def = get_structured_value(0x027C, 0xD1)
        assert value_def is not None
        assert decode_property_value(value_def, b"\xff" * 4) == "undefined"


class TestDecodePropertyValueArray:
    """Tests for :func:`decode_property_value` on :class:`ArrayDefinition`."""

    def test_decodes_fixed_size_items(self) -> None:
        """Items decode in order, sized by ``item_size``."""
        value_def = ArrayDefinition(
            item=ScalarDefinition(size=1, format="uint8"), item_size=1
        )
        assert decode_property_value(value_def, b"\x01\x02\x03") == (1, 2, 3)

    def test_length_not_multiple_of_item_size_returns_none(self) -> None:
        """A chunk whose length is not a multiple of ``item_size`` is rejected."""
        value_def = ArrayDefinition(
            item=ScalarDefinition(size=2, format="uint16"), item_size=2
        )
        assert decode_property_value(value_def, b"\x00\x01\x02") is None

    def test_exceeding_max_items_returns_none(self) -> None:
        """An item count above ``max_items`` is rejected."""
        value_def = ArrayDefinition(
            item=ScalarDefinition(size=1, format="uint8"), item_size=1, max_items=2
        )
        assert decode_property_value(value_def, b"\x01\x02\x03") is None

    def test_below_min_items_returns_none(self) -> None:
        """An item count below ``min_items`` is rejected."""
        value_def = ArrayDefinition(
            item=ScalarDefinition(size=1, format="uint8"), item_size=1, min_items=2
        )
        assert decode_property_value(value_def, b"\x01") is None

    def test_empty_chunk_decodes_to_empty_tuple(self) -> None:
        """An empty chunk decodes to an empty tuple of items."""
        value_def = ArrayDefinition(
            item=ScalarDefinition(size=1, format="uint8"), item_size=1
        )
        assert decode_property_value(value_def, b"") == ()


class TestValueDefinitionByteSize:
    """Tests for :func:`_value_definition_byte_size`."""

    def test_scalar_size(self) -> None:
        """A scalar's byte size is its declared ``size``."""
        assert _value_definition_byte_size(ScalarDefinition(size=4)) == 4

    def test_object_size_is_sum_of_fields(self) -> None:
        """An object's byte size is the sum of its fields' sizes."""
        value_def = ObjectDefinition(
            fields=(
                ObjectField(
                    key="a", name_en="a", name_ja="a", value=ScalarDefinition(size=1)
                ),
                ObjectField(
                    key="b", name_en="b", name_ja="b", value=ScalarDefinition(size=4)
                ),
            )
        )
        assert _value_definition_byte_size(value_def) == 5

    def test_one_of_requires_matching_sizes(self) -> None:
        """A oneOf's byte size is well-defined when all options agree."""
        value_def = OneOfDefinition(
            options=(ScalarDefinition(size=1), ScalarDefinition(size=1))
        )
        assert _value_definition_byte_size(value_def) == 1

    def test_one_of_mismatched_sizes_raises(self) -> None:
        """A oneOf with disagreeing option sizes raises :class:`ValueError`."""
        value_def = OneOfDefinition(
            options=(ScalarDefinition(size=1), ScalarDefinition(size=4))
        )
        with pytest.raises(ValueError, match="same byte size"):
            _value_definition_byte_size(value_def)

    def test_array_has_no_fixed_size(self) -> None:
        """An array's byte size is undefined (variable-length)."""
        value_def = ArrayDefinition(item=ScalarDefinition(size=1), item_size=1)
        with pytest.raises(ValueError, match="no fixed byte size"):
            _value_definition_byte_size(value_def)


class TestDecodeCollection:
    """Tests for :func:`_decode_collection` page normalization."""

    _BINDING = CollectionBinding(
        result_epc=0xBE,
        count_epc=0xB8,
        items_path=("items",),
        start_path=("start",),
        page_count_path=("count",),
    )

    def test_normalizes_valid_page(self) -> None:
        """A consistent header/items pair normalizes to a :class:`CollectionPage`."""
        page = _decode_collection(
            self._BINDING, {"start": 1, "count": 2, "items": (10, 20)}
        )
        assert page == CollectionPage(start=1, count=2, items=(10, 20))

    def test_rejects_count_mismatch(self) -> None:
        """A declared count that disagrees with the actual item count is rejected."""
        assert (
            _decode_collection(
                self._BINDING, {"start": 1, "count": 3, "items": (10, 20)}
            )
            is None
        )

    def test_rejects_missing_header(self) -> None:
        """A missing start/count header is rejected."""
        assert _decode_collection(self._BINDING, {"items": (10, 20)}) is None

    def test_rejects_non_tuple_items(self) -> None:
        """A missing or malformed items list is rejected."""
        assert (
            _decode_collection(self._BINDING, {"start": 1, "count": 0, "items": None})
            is None
        )


class TestCollectionRegistryLookups:
    """Tests for :func:`get_structured_value`/:func:`get_collection_binding`."""

    def test_get_structured_value_returns_none_when_absent(self) -> None:
        """A class/EPC with no curated structured value returns ``None``."""
        assert get_structured_value(0x0130, 0x80) is None

    def test_get_collection_binding_returns_none_when_absent(self) -> None:
        """A class/EPC with no curated collection binding returns ``None``."""
        assert get_collection_binding(0x0130, 0x80) is None

    @pytest.mark.parametrize("epc", [0xB3, 0xB7, 0xBA, 0xBE])
    def test_0287_collection_epcs_have_structured_value_and_binding(
        self, epc: int
    ) -> None:
        """Every v2-scoped 0x0287 list EPC has both a value tree and a binding."""
        assert get_structured_value(0x0287, epc) is not None
        assert get_collection_binding(0x0287, epc) is not None


class TestDecodeCollectionPageFor0287:
    """End-to-end :func:`decode_collection_page` tests for class 0x0287.

    Covers the branch-circuit metering array properties.
    """

    @staticmethod
    def _header(start: int, count: int) -> bytes:
        return bytes([start, count])

    def test_simplex_instantaneous_power_list(self) -> None:
        """Simplex power list (0xB7) decodes a value and a ``noData`` sentinel."""
        edt = (
            self._header(1, 2) + struct.pack(">i", 100) + struct.pack(">I", 0x7FFFFFFE)
        )
        page = decode_collection_page(0x0287, 0xB7, edt)
        assert page == CollectionPage(start=1, count=2, items=(100, "noData"))

    def test_duplex_instantaneous_power_list(self) -> None:
        """Duplex power list (0xBE) decodes a negative (reverse-flow) value."""
        edt = self._header(3, 1) + struct.pack(">i", -50)
        page = decode_collection_page(0x0287, 0xBE, edt)
        assert page == CollectionPage(start=3, count=1, items=(-50,))

    def test_simplex_cumulative_energy_resolves_coefficient(self) -> None:
        """Simplex energy list (0xB3) applies the 0xC2 coefficient via ``context``."""
        context = {0xC2: b"\x02"}  # x0.01
        edt = self._header(1, 1) + struct.pack(">I", 500)
        page = decode_collection_page(0x0287, 0xB3, edt, context)
        assert page == CollectionPage(start=1, count=1, items=(pytest.approx(5.0),))

    def test_simplex_cumulative_energy_without_context_is_none_item(self) -> None:
        """Without ``context``, a coefficient-bearing item decodes to ``None``."""
        edt = self._header(1, 1) + struct.pack(">I", 500)
        page = decode_collection_page(0x0287, 0xB3, edt)
        assert page == CollectionPage(start=1, count=1, items=(None,))

    def test_duplex_cumulative_energy_forward_and_reverse(self) -> None:
        """Duplex energy list (0xBA) decodes forward/reverse fields independently."""
        context = {0xC2: b"\x00"}  # x1
        edt = (
            self._header(1, 1)
            + struct.pack(">I", 12)
            + struct.pack(">I", 0xFFFFFFFE)  # reverse: no data
        )
        page = decode_collection_page(0x0287, 0xBA, edt, context)
        assert page is not None
        assert page.start == 1
        assert page.count == 1
        item = page.items[0]
        assert item["normalDirectionElectricEnergy"] == pytest.approx(12.0)
        assert item["reverseDirectionElectricEnergy"] == "noData"

    def test_rejects_page_count_mismatch(self) -> None:
        """A page whose declared range disagrees with actual item count is rejected."""
        edt = self._header(1, 5) + struct.pack(">i", 100)
        assert decode_collection_page(0x0287, 0xB7, edt) is None

    def test_rejects_zero_range(self) -> None:
        """A zero-range page (below the MRA minimum of 1) is rejected."""
        edt = self._header(1, 0)
        assert decode_collection_page(0x0287, 0xB7, edt) is None

    def test_rejects_oversized_page(self) -> None:
        """A page exceeding the MRA per-EPC item limit is rejected."""
        # 61 items exceeds MRA maxItems=60 for the simplex power list
        edt = self._header(1, 60) + b"\x00\x00\x00\x64" * 61
        assert decode_collection_page(0x0287, 0xB7, edt) is None

    def test_unknown_epc_returns_none(self) -> None:
        """An EPC with no curated binding/value tree returns ``None``."""
        assert decode_collection_page(0x0287, 0xFF, b"\x00") is None


class TestGetCodecForEpcSelection:
    """Tests for the manufacturer and byte offset selection of get_codec_for_epc."""

    def test_selects_field_by_byte_offset(self) -> None:
        """Each packed field of one EPC has its own codec."""
        first = get_codec_for_epc(0x0130, 0xB8)
        second = get_codec_for_epc(0x0130, 0xB8, byte_offset=2)
        assert isinstance(first, NumericCodec)
        assert isinstance(second, NumericCodec)
        assert first.byte_offset == 0
        assert second.byte_offset == 2

    def test_manufacturer_specific_definition(self) -> None:
        """Vendor definitions are returned only for their manufacturer."""
        codec = get_codec_for_epc(
            0x0135, 0xF1, manufacturer_code=0x000005, byte_offset=3
        )
        assert isinstance(codec, NumericCodec)
        assert codec.mra_format == "int8"
        assert codec.byte_offset == 3
        with pytest.raises(LookupError):
            get_codec_for_epc(0x0135, 0xF1, byte_offset=3)
        with pytest.raises(LookupError):
            get_codec_for_epc(0x0135, 0xF1, manufacturer_code=0x000006, byte_offset=3)

    def test_falls_back_to_generic_definition(self) -> None:
        """A manufacturer without its own definition uses the generic one."""
        assert get_codec_for_epc(
            0x0130, 0x80, manufacturer_code=0x000005
        ) == get_codec_for_epc(0x0130, 0x80)


@pytest.mark.parametrize(
    ("codec", "value"),
    [
        (BinaryCodec(on_edt=0x30, off_edt=0x31, byte_offset=1), True),
        (EnumCodec(by_key={"a": 1}, by_edt={1: "a"}, byte_offset=1), "a"),
        (NumericValueCodec(by_edt={0x00: 1.0}, byte_offset=1), 1.0),
    ],
)
def test_encode_with_byte_offset_rejected(codec: object, value: object) -> None:
    """Every codec refuses to encode a field that does not start the EDT."""
    with pytest.raises(ValueError, match="byte_offset"):
        codec.encode(value)  # type: ignore[attr-defined]


class TestTimeCodec:
    """Tests for :class:`TimeCodec`."""

    def test_get_codec_returns_time_for_time_format(self) -> None:
        """get_codec selects TimeCodec for format='time'."""
        entity = EntityDefinition(
            id="class_0130_epc_91",
            epc=0x91,
            name_en="test",
            name_ja="test",
            readable=True,
            writable=True,
            format="time",
            time_size=2,
        )
        assert isinstance(get_codec(entity), TimeCodec)

    def test_decode_hh_mm(self) -> None:
        """HH:MM EDT decodes to datetime.time."""
        codec = TimeCodec(size=2)
        assert codec.decode(b"\x07\x30") == datetime.time(7, 48)

    def test_decode_with_seconds(self) -> None:
        """A size-3 EDT includes seconds."""
        codec = TimeCodec(size=3)
        assert codec.decode(b"\x07\x30\x1e") == datetime.time(7, 48, 30)

    def test_decode_sentinel_returns_none(self) -> None:
        """Hour 0xFF with maximumOfHour 255 decodes to None."""
        codec = TimeCodec(size=2, max_hour=0xFF)
        assert codec.decode(b"\xff\xff") is None

    def test_decode_sentinel_hour_is_invalid_without_max_hour(self) -> None:
        """Hour 0xFF is invalid without the sentinel variant."""
        codec = TimeCodec(size=2)
        assert codec.decode(b"\xff\xff") is None

    @pytest.mark.parametrize(
        ("size", "edt"),
        [
            (2, b""),
            (2, b"\x07"),
            (2, b"\x18\x00"),
            (2, b"\x07\x3c"),
            (3, b"\x07\x30"),
            (3, b"\x07\x30\x3c"),
        ],
    )
    def test_decode_invalid_returns_none(self, size: int, edt: bytes) -> None:
        """Short or out-of-range EDTs decode to None."""
        assert TimeCodec(size=size).decode(edt) is None

    def test_decode_with_byte_offset(self) -> None:
        """byte_offset skips leading EDT bytes."""
        codec = TimeCodec(size=2, byte_offset=1)
        assert codec.decode(b"\x00\x07\x30") == datetime.time(7, 48)

    def test_encode_decode_round_trip(self) -> None:
        """datetime.time round-trips through EDT bytes."""
        codec = TimeCodec(size=2)
        assert codec.decode(codec.encode(datetime.time(23, 59))) == datetime.time(
            23, 59
        )
        codec3 = TimeCodec(size=3)
        assert codec3.decode(codec3.encode(datetime.time(1, 2, 3))) == datetime.time(
            1, 2, 3
        )

    def test_encode_none_writes_sentinel(self) -> None:
        """None encodes to the hour-0xFF sentinel when allowed."""
        codec = TimeCodec(size=2, max_hour=0xFF)
        assert codec.encode(None) == b"\xff\xff"

    def test_encode_none_without_sentinel_raises(self) -> None:
        """None is rejected without the sentinel variant."""
        with pytest.raises(ValueError, match="no setting"):
            TimeCodec(size=2).encode(None)

    def test_encode_wrong_type_raises(self) -> None:
        """Non-time values are rejected."""
        with pytest.raises(ValueError, match=r"datetime\.time"):
            TimeCodec(size=2).encode("07:30")  # type: ignore[arg-type]

    def test_encode_with_byte_offset_rejected(self) -> None:
        """Encoding at a non-zero byte_offset is rejected."""
        with pytest.raises(ValueError, match="byte_offset"):
            TimeCodec(size=2, byte_offset=1).encode(datetime.time(7, 48))

    def test_generated_home_air_conditioner_timers(self) -> None:
        """The MRA timer properties of class 0x0130 get working codecs."""
        on_time = get_codec_for_epc(0x0130, 0x91)
        assert isinstance(on_time, TimeCodec)
        assert on_time.decode(b"\x07\x30") == datetime.time(7, 48)

        on_relative = get_codec_for_epc(0x0130, 0x92)
        assert isinstance(on_relative, TimeCodec)
        assert on_relative.decode(b"\xff\xff") is None
        assert on_relative.encode(None) == b"\xff\xff"
