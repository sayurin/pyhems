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

import functools
import math
from dataclasses import dataclass
from typing import Any, Protocol

from ._definitions_generated import REGISTRY
from .definitions import (
    ArrayDefinition,
    CollectionBinding,
    EntityDefinition,
    ObjectDefinition,
    OneOfDefinition,
    PropertyValueDefinition,
    ScalarDefinition,
)
from .device_manager import NodeState
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
    byte_offset: int = 0

    def decode(self, edt: bytes) -> bool | None:
        """Return ``True``/``False`` for the configured ON/OFF bytes."""
        if not edt or self.byte_offset >= len(edt):
            return None
        val = edt[self.byte_offset]
        if val == self.on_edt:
            return True
        if val == self.off_edt:
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
    byte_offset: int = 0

    @classmethod
    def from_mapping(cls, forward: dict[str, int]) -> EnumCodec:
        """Build an EnumCodec from a single forward key→EDT mapping.

        The reverse EDT→key mapping is derived automatically.
        """
        return cls(by_key=forward, by_edt={v: k for k, v in forward.items()})

    def decode(self, edt: bytes) -> str | None:
        """Return the ``key`` matching the EDT byte at ``byte_offset``, or ``None``."""
        if not edt or self.byte_offset >= len(edt):
            return None
        return self.by_edt.get(edt[self.byte_offset])

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

    ``coefficient_epcs`` handles the MRA ``coefficient`` pattern (e.g. EPC
    0xC2 "Unit for cumulative amounts of electric energy"): the true value is
    this codec's raw value multiplied by the *current* value of one or more
    sibling EPCs. Resolving it requires the owning node's state, so ``decode``
    accepts an optional ``node`` argument used only when ``coefficient_epcs``
    is non-empty; without it (or when the coefficient EPC is not yet known),
    ``None`` is returned rather than an unscaled, potentially wrong value.
    """

    mra_format: str
    scale: float
    minimum: float | None
    maximum: float | None
    byte_offset: int
    coefficient_epcs: tuple[int, ...] = ()

    def decode(self, edt: bytes, node: NodeState | None = None) -> int | float | None:
        """Decode EDT bytes to a (scaled) numeric value.

        ``node`` supplies the current values of ``coefficient_epcs`` when
        this property's unit depends on a sibling property; it is ignored
        when ``coefficient_epcs`` is empty.
        """
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
        value = raw if self.scale == 1.0 else raw * self.scale
        if not self.coefficient_epcs:
            return value
        if node is None:
            return None
        coefficient = 1.0
        for coef_epc in self.coefficient_epcs:
            coef_edt = node.properties.get(coef_epc)
            if coef_edt is None:
                return None
            coef_value = get_codec_for_epc(node.eoj.class_code, coef_epc).decode(
                coef_edt
            )
            if coef_value is None:
                return None
            coefficient *= coef_value
        return value * coefficient

    def encode(self, value: int | float) -> bytes:
        """Encode a numeric value to EDT bytes.

        ``byte_offset`` is not supported here because writing only a portion
        of a multi-field EDT requires merging with the device's current
        value, which is the caller's responsibility.
        """
        if self.coefficient_epcs:
            raise ValueError(
                "NumericCodec.encode does not support coefficient_epcs; "
                "resolving the coefficient requires node state, which "
                "encode() does not receive"
            )
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
class NumericValueCodec:
    """Codec for MRA ``numericValue`` properties exchanging ``float`` values.

    Used for unit/coefficient properties (e.g. EPC 0xC2) whose EDT byte
    selects a multiplying factor from a fixed table rather than encoding a
    magnitude directly. Analogous to :class:`EnumCodec`, but maps to ``float``
    values instead of ``str`` keys.
    """

    by_edt: dict[int, float]
    byte_offset: int = 0

    def decode(self, edt: bytes) -> float | None:
        """Return the numeric value matching the EDT byte at ``byte_offset``."""
        if not edt or self.byte_offset >= len(edt):
            return None
        return self.by_edt.get(edt[self.byte_offset])

    def encode(self, value: float) -> bytes:
        """Encode a numeric value to the matching EDT byte.

        Matches by :func:`math.isclose` rather than exact equality, since
        ``value`` may not be the same float object/computation that produced
        a table entry (e.g. round-tripped through JSON or user input).
        """
        for edt, val in self.by_edt.items():
            if math.isclose(val, value, rel_tol=1e-9):
                return bytes([edt])
        raise ValueError(f"Unknown numeric value: {value!r}")


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

    * A populated ``numeric_values`` field produces a :class:`NumericValueCodec`.
    * ``enum_values`` with normalized ``key="true"``/``"false"`` values
      produces a :class:`BinaryCodec`.
    * Any other non-empty ``enum_values`` produces an :class:`EnumCodec`.
    * A populated ``format`` field produces a :class:`NumericCodec`.

    Raises :class:`ValueError` when no codec can be selected (for example
    when ``enum_values``, ``format``, and ``numeric_values`` are all absent).
    """
    if entity_def.numeric_values:
        return NumericValueCodec(
            by_edt={nv.edt: nv.value for nv in entity_def.numeric_values},
            byte_offset=entity_def.byte_offset,
        )

    if entity_def.enum_values:
        if entity_def.is_binary:
            on_edt, off_edt = entity_def.get_binary_values()
            return BinaryCodec(
                on_edt=on_edt[0],
                off_edt=off_edt[0],
                byte_offset=entity_def.byte_offset,
            )
        return EnumCodec(
            by_key={ev.key: ev.edt for ev in entity_def.enum_values},
            by_edt={ev.edt: ev.key for ev in entity_def.enum_values},
            byte_offset=entity_def.byte_offset,
        )

    if entity_def.format:
        return NumericCodec(
            mra_format=entity_def.format,
            scale=entity_def.multiple_of,
            minimum=entity_def.minimum,
            maximum=entity_def.maximum,
            byte_offset=entity_def.byte_offset,
            coefficient_epcs=entity_def.coefficient_epcs or (),
        )

    raise ValueError(
        f"Cannot determine codec for EPC 0x{entity_def.epc:02X}: "
        "neither enum_values, format, nor numeric_values is set"
    )


@functools.cache
def get_codec_for_epc(
    class_code: int,
    epc: int,
) -> PropertyCodec:
    """Return the appropriate codec for *epc* on device class *class_code*.

    Looks up the :class:`~pyhems.EntityDefinition` for the given EPC in
    :data:`pyhems.REGISTRY` and calls :func:`get_codec` on it.

    Results are cached (keyed by ``class_code``/``epc``) since :data:`REGISTRY`
    is an immutable singleton built once at import time; this avoids repeating
    the linear scan and :class:`PropertyCodec` construction on every call,
    which matters for :class:`NumericCodec`'s ``coefficient_epcs`` resolution
    (invoked on every ``decode()`` of a coefficient-bearing property).

    Raises :class:`LookupError` when no definition exists for the EPC on the
    given class.  Propagates :class:`ValueError` from :func:`get_codec` when
    no codec can be built from the definition (e.g. neither ``enum_values``
    nor ``format`` is set).
    """
    for entity_def in REGISTRY.entities.get(class_code, ()):
        if entity_def.epc == epc:
            return get_codec(entity_def)
    raise LookupError(f"EPC 0x{epc:02X} not found for class 0x{class_code:04X}")


# ============================================================================
# Generic structured value decoding
#
# Complements the scalar EntityDefinition/PropertyCodec model above with a
# recursive decoder for MRA properties that describe a variable-length list,
# optionally wrapping named fields or alternative interpretations (see
# pyhems.definitions.PropertyValueDefinition). A single generic decoder walks
# any such tree instead of adding a bespoke Codec per property.
# ============================================================================


def _scalar_byte_size(value_def: ScalarDefinition) -> int:
    """Return the byte width declared for a scalar leaf value."""
    return value_def.size


def value_definition_byte_size(value_def: PropertyValueDefinition) -> int:
    """Return the fixed byte width of *value_def*.

    Raises :class:`ValueError` when *value_def* has no fixed width (i.e. it
    is, or contains, an :class:`ArrayDefinition`) or when a
    :class:`OneOfDefinition`'s options disagree on their byte width.
    """
    if isinstance(value_def, ScalarDefinition):
        return _scalar_byte_size(value_def)
    if isinstance(value_def, OneOfDefinition):
        sizes = {value_definition_byte_size(option) for option in value_def.options}
        if len(sizes) != 1:
            raise ValueError("oneOf options must share the same byte size")
        return next(iter(sizes))
    if isinstance(value_def, ObjectDefinition):
        return sum(value_definition_byte_size(f.value) for f in value_def.fields)
    raise ValueError("ArrayDefinition has no fixed byte size")


def _decode_scalar(
    value_def: ScalarDefinition, chunk: bytes, node: NodeState | None
) -> Any:
    """Decode a single scalar leaf value from *chunk*."""
    if len(chunk) != value_def.size:
        return None
    if value_def.numeric_values is not None:
        return NumericValueCodec(
            by_edt={nv.edt: nv.value for nv in value_def.numeric_values}
        ).decode(chunk)
    if value_def.enum_values:
        if len(chunk) < value_def.size:
            return None
        raw = int.from_bytes(chunk[: value_def.size], "big", signed=False)
        for ev in value_def.enum_values:
            if ev.edt == raw:
                return ev.key
        return None
    if value_def.format:
        return NumericCodec(
            mra_format=value_def.format,
            scale=value_def.multiple_of,
            minimum=value_def.minimum,
            maximum=value_def.maximum,
            byte_offset=0,
            coefficient_epcs=value_def.coefficient_epcs,
        ).decode(chunk, node)
    return chunk


def _fixed_size_options(
    value_def: PropertyValueDefinition,
) -> tuple[tuple[PropertyValueDefinition, int], ...]:
    """Return each fixed-size decoding option for a value definition."""
    if isinstance(value_def, OneOfDefinition):
        return tuple(
            (option, value_definition_byte_size(option)) for option in value_def.options
        )
    return ((value_def, value_definition_byte_size(value_def)),)


def decode_property_value(
    value_def: PropertyValueDefinition,
    chunk: bytes,
    node: NodeState | None = None,
) -> Any:
    """Recursively decode *chunk* according to *value_def*.

    Returns ``None`` when *chunk* is malformed for *value_def* (wrong
    length, an array exceeding its declared ``max_items``/``min_items``, or
    no matching :class:`OneOfDefinition` option) rather than raising, so
    callers can treat any structured property the same way scalar
    :class:`PropertyCodec` implementations already do.

    * :class:`ScalarDefinition` decodes to ``int``/``float``/``str``/``None``,
      mirroring :class:`NumericCodec`/:class:`EnumCodec`/:class:`NumericValueCodec`.
    * :class:`OneOfDefinition` tries each option in order and returns the
      first non-``None`` result.
    * :class:`ObjectDefinition` decodes to a ``dict`` keyed by field ``key``.
      A trailing :class:`ArrayDefinition` field consumes all remaining bytes
      of *chunk* instead of a fixed width.
    * :class:`ArrayDefinition` decodes to a ``tuple`` of item values.
    """
    if isinstance(value_def, ScalarDefinition):
        return _decode_scalar(value_def, chunk, node)

    if isinstance(value_def, OneOfDefinition):
        for option in value_def.options:
            option_result = decode_property_value(option, chunk, node)
            if option_result is not None:
                return option_result
        return None

    if isinstance(value_def, ObjectDefinition):

        def decode_fields(field_index: int, cursor: int) -> dict[str, Any] | None:
            if field_index == len(value_def.fields):
                return {} if cursor == len(chunk) else None

            field = value_def.fields[field_index]
            if isinstance(field.value, ArrayDefinition):
                if field_index != len(value_def.fields) - 1:
                    return None
                item_value = decode_property_value(field.value, chunk[cursor:], node)
                return {field.key: item_value} if item_value is not None else None

            try:
                options = _fixed_size_options(field.value)
            except ValueError:
                return None
            for _option, size in options:
                next_cursor = cursor + size
                if next_cursor > len(chunk):
                    continue
                remaining = decode_fields(field_index + 1, next_cursor)
                if remaining is None:
                    continue
                return {
                    field.key: decode_property_value(
                        field.value, chunk[cursor:next_cursor], node
                    ),
                    **remaining,
                }
            return None

        return decode_fields(0, 0)

    if isinstance(value_def, ArrayDefinition):
        item_size = value_def.item_size
        if item_size <= 0 or len(chunk) % item_size != 0:
            return None
        count = len(chunk) // item_size
        if value_def.max_items is not None and count > value_def.max_items:
            return None
        if value_def.min_items is not None and count < value_def.min_items:
            return None
        return tuple(
            decode_property_value(
                value_def.item, chunk[i * item_size : (i + 1) * item_size], node
            )
            for i in range(count)
        )

    raise TypeError(f"Unsupported value definition: {value_def!r}")


@dataclass(frozen=True, slots=True)
class CollectionPage:
    """A normalized page of a paged list result.

    Attributes:
        start: 1-based index of the first item in ``items``.
        count: Number of items in ``items`` (matches the decoded header).
        items: Decoded item values, in index order starting at ``start``.
    """

    start: int
    count: int
    items: tuple[Any, ...]


def _get_path(value: Any, path: tuple[str, ...]) -> Any:
    """Walk a sequence of dict keys, returning None if any step is missing."""
    node = value
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def decode_collection(binding: CollectionBinding, value: Any) -> CollectionPage | None:
    """Normalize a decoded structured *value* into a :class:`CollectionPage`.

    Returns ``None`` when the header (``start_path``/``page_count_path``) is
    missing or invalid, or when the declared page count does not match the
    actual number of decoded items — the whole page is rejected rather than
    exposing a partially-trustworthy result.
    """
    items = _get_path(value, binding.items_path)
    if not isinstance(items, tuple):
        return None
    start = _get_path(value, binding.start_path)
    page_count = _get_path(value, binding.page_count_path)
    if not isinstance(start, int) or not isinstance(page_count, int):
        return None
    if page_count != len(items):
        return None
    return CollectionPage(start=start, count=page_count, items=items)


def get_structured_value(class_code: int, epc: int) -> PropertyValueDefinition | None:
    """Return the curated :class:`PropertyValueDefinition` for *epc*, if any."""
    return REGISTRY.structured_values.get(class_code, {}).get(epc)


def get_collection_binding(
    class_code: int, result_epc: int
) -> CollectionBinding | None:
    """Return the curated :class:`CollectionBinding` for *result_epc*, if any."""
    for binding in REGISTRY.collection_bindings.get(class_code, ()):
        if binding.result_epc == result_epc:
            return binding
    return None


def decode_collection_page(
    class_code: int,
    result_epc: int,
    edt: bytes,
    node: NodeState | None = None,
) -> CollectionPage | None:
    """Decode and normalize *edt* for a paged list property in one call.

    Returns ``None`` when *class_code*/*result_epc* has no curated
    :class:`CollectionBinding`, no :class:`PropertyValueDefinition`, or *edt*
    fails to decode/normalize into a valid page.
    """
    value_def = get_structured_value(class_code, result_epc)
    binding = get_collection_binding(class_code, result_epc)
    if value_def is None or binding is None:
        return None
    value = decode_property_value(value_def, edt, node)
    if value is None:
        return None
    return decode_collection(binding, value)


__all__ = [
    "BinaryCodec",
    "CollectionPage",
    "EnumCodec",
    "InstallationLocationCodec",
    "NumericCodec",
    "NumericValueCodec",
    "PropertyCodec",
    "decode_collection",
    "decode_collection_page",
    "decode_property_value",
    "get_codec",
    "get_codec_for_epc",
    "get_collection_binding",
    "get_structured_value",
    "value_definition_byte_size",
]
