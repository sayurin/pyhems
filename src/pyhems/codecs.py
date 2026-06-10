"""High-level value codecs for ECHONET Lite properties.

This module wraps the lower-level decoder/encoder factories in
:mod:`pyhems.definitions` behind a uniform :class:`PropertyCodec` protocol
and exposes :func:`get_codec`, which selects the appropriate codec from an
:class:`EntityDefinition`.

The goal is to let callers exchange typed Python values (``bool``, ``int``,
``float``, ``str`` enum key, :class:`InstallationLocation`) with a node
without having to assemble raw EDT bytes themselves.

Typical usage::

    from pyhems import get_codec

    codec = get_codec(entity_def)
    value = codec.decode(edt_bytes)        # bytes -> typed value
    edt = codec.encode(True)               # typed value -> bytes
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ._definitions_generated import REGISTRY
from .definitions import EntityDefinition
from .installation_location import (
    INSTALLATION_LOCATIONS,
    InstallationLocation,
    decode_installation_location,
)

# MRA format string -> (signed, byte_count)
_FORMAT_INFO: dict[str, tuple[bool, int]] = {
    "uint8": (False, 1),
    "int8": (True, 1),
    "uint16": (False, 2),
    "int16": (True, 2),
    "uint32": (False, 4),
    "int32": (True, 4),
}


class PropertyCodec(Protocol):
    """Bidirectional EDT bytes ⇔ typed Python value conversion."""

    def decode(self, edt: bytes) -> Any:
        """Decode raw EDT bytes to a typed Python value.

        Returns ``None`` when the bytes are empty or do not represent a
        valid value for this codec.
        """

    def encode(self, value: Any) -> bytes:
        """Encode a typed Python value to raw EDT bytes.

        Raises :class:`ValueError` when the value is outside the supported
        domain.
        """


@dataclass(frozen=True, slots=True)
class BinaryCodec:
    """ON/OFF codec exchanging :class:`bool` values.

    Built from the ``key="true"`` / ``key="false"`` :class:`EnumValue`
    entries of an :class:`EntityDefinition`.
    """

    on_edt: int
    off_edt: int

    def decode(self, edt: bytes) -> bool | None:
        """Return ``True``/``False`` for the configured ON/OFF bytes."""
        if not edt:
            return None
        first = edt[0]
        if first == self.on_edt:
            return True
        if first == self.off_edt:
            return False
        return None

    def encode(self, value: bool) -> bytes:
        """Encode a ``bool`` to the matching ON/OFF byte."""
        return bytes([self.on_edt if value else self.off_edt])


@dataclass(frozen=True, slots=True)
class EnumCodec:
    """Multi-state codec exchanging :class:`str` keys from ``enum_values``."""

    by_key: dict[str, int]
    by_edt: dict[int, str]

    @classmethod
    def from_mapping(cls, forward: dict[str, int]) -> EnumCodec:
        """Build an EnumCodec from a single forward key→EDT mapping.

        The reverse EDT→key mapping is derived automatically.
        """
        return cls(by_key=forward, by_edt={v: k for k, v in forward.items()})

    def decode(self, edt: bytes) -> str | None:
        """Return the ``key`` matching the first EDT byte, or ``None``."""
        if not edt:
            return None
        return self.by_edt.get(edt[0])

    def encode(self, value: str) -> bytes:
        """Encode a ``key`` from ``enum_values`` to the matching byte."""
        try:
            return bytes([self.by_key[value]])
        except KeyError as ex:
            raise ValueError(f"Unknown enum key: {value!r}") from ex


@dataclass(frozen=True, slots=True)
class NumericCodec:
    """Numeric codec exchanging ``int`` or ``float`` values.

    Handles MRA ``format`` (``uint8``/``int16``/...), ``minimum``/``maximum``
    range checks, and ``multipleOf`` scaling. ``byte_offset`` lets a single
    EPC expose several numeric fields packed in one EDT.
    """

    mra_format: str
    scale: float
    minimum: float | None
    maximum: float | None
    byte_offset: int

    def decode(self, edt: bytes) -> int | float | None:
        """Decode EDT bytes to a (scaled) numeric value."""
        format_info = _FORMAT_INFO.get(self.mra_format)
        if not format_info:
            return None
        signed, byte_count = format_info
        required_len = self.byte_offset + byte_count
        if not edt or len(edt) < required_len:
            return None
        raw = int.from_bytes(
            edt[self.byte_offset : self.byte_offset + byte_count], "big", signed=signed
        )
        if self.minimum is not None and raw < self.minimum:
            return None
        if self.maximum is not None and raw > self.maximum:
            return None
        return raw if self.scale == 1.0 else raw * self.scale

    def encode(self, value: int | float) -> bytes:
        """Encode a numeric value to EDT bytes.

        ``byte_offset`` is not supported here because writing only a portion
        of a multi-field EDT requires merging with the device's current
        value, which is the caller's responsibility.
        """
        if self.byte_offset:
            raise ValueError(
                "NumericCodec.encode does not support byte_offset; "
                "compose the full EDT in the caller"
            )
        format_info = _FORMAT_INFO.get(self.mra_format)
        if not format_info:
            raise ValueError(f"Unknown MRA format: {self.mra_format}")
        signed, byte_count = format_info
        raw = round(value / self.scale) if self.scale != 1.0 else round(value)
        try:
            return int(raw).to_bytes(byte_count, "big", signed=signed)
        except OverflowError as ex:
            raise ValueError(
                f"Value {value} out of range for format {self.mra_format}"
            ) from ex


@dataclass(frozen=True, slots=True)
class InstallationLocationCodec:
    r"""Codec for EPC 0x81 exchanging :class:`InstallationLocation`.

    Encoding requires a fully populated :class:`InstallationLocation`. To
    write the "unset" or "indefinite" sentinel bytes, callers should send
    the raw byte directly (``b"\x00"`` or ``b"\xff"``) via the low-level
    API; this codec only handles standard ``LLLL``/``NNN`` values.
    """

    def decode(self, edt: bytes) -> InstallationLocation | None:
        """Decode EDT bytes to an :class:`InstallationLocation`."""
        return decode_installation_location(edt)

    def encode(self, value: InstallationLocation) -> bytes:
        """Encode an :class:`InstallationLocation` to a single EDT byte."""
        if value.code not in INSTALLATION_LOCATIONS:
            raise ValueError(f"Unknown installation location code: {value.code}")
        if not 0 <= value.instance <= 0x07:
            raise ValueError(
                f"Installation location instance must be 0..7, got {value.instance}"
            )
        return bytes([((value.code & 0x0F) << 3) | (value.instance & 0x07)])


def get_codec(entity_def: EntityDefinition) -> PropertyCodec:
    """Return the appropriate :class:`PropertyCodec` for ``entity_def``.

    Selection rules:

    * Exactly two ``enum_values`` produces a :class:`BinaryCodec`.
      The ON/OFF assignment follows :meth:`EntityDefinition.get_binary_values`:
      ``key="true"``/``"false"`` is preferred, otherwise the first value is
      treated as ON and the second as OFF.
    * Any other non-empty ``enum_values`` produces an :class:`EnumCodec`.
    * A populated ``format`` field produces a :class:`NumericCodec`.

    Raises :class:`ValueError` when no codec can be selected (for example
    when both ``enum_values`` and ``format`` are absent).
    """
    if entity_def.enum_values:
        if len(entity_def.enum_values) == 2:
            on_edt, off_edt = entity_def.get_binary_values()
            return BinaryCodec(on_edt=on_edt[0], off_edt=off_edt[0])
        return EnumCodec(
            by_key={ev.key: ev.edt for ev in entity_def.enum_values},
            by_edt={ev.edt: ev.key for ev in entity_def.enum_values},
        )

    if entity_def.format:
        return NumericCodec(
            mra_format=entity_def.format,
            scale=entity_def.multiple_of,
            minimum=entity_def.minimum,
            maximum=entity_def.maximum,
            byte_offset=entity_def.byte_offset,
        )

    raise ValueError(
        f"Cannot determine codec for EPC 0x{entity_def.epc:02X}: "
        "neither enum_values nor format is set"
    )


def get_codec_for_epc(
    class_code: int,
    epc: int,
) -> PropertyCodec:
    """Return the appropriate codec for *epc* on device class *class_code*.

    Looks up the :class:`~pyhems.EntityDefinition` for the given EPC in
    :data:`pyhems.REGISTRY` and calls :func:`get_codec` on it.

    Raises :class:`LookupError` when no definition exists for the EPC on the
    given class.  Propagates :class:`ValueError` from :func:`get_codec` when
    no codec can be built from the definition (e.g. neither ``enum_values``
    nor ``format`` is set).
    """
    for entity_def in REGISTRY.entities.get(class_code, ()):
        if entity_def.epc == epc:
            return get_codec(entity_def)
    raise LookupError(f"EPC 0x{epc:02X} not found for class 0x{class_code:04X}")


__all__ = [
    "BinaryCodec",
    "EnumCodec",
    "InstallationLocationCodec",
    "NumericCodec",
    "PropertyCodec",
    "get_codec",
    "get_codec_for_epc",
]
