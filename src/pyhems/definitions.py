"""ECHONET Lite definitions for entity creation.

This module provides the dataclasses describing ECHONET Lite devices:
- EntityDefinition for entity configuration
- DeviceDefinition for device class configuration
- DefinitionsRegistry for managing all definitions

The concrete data is generated as code in ``_definitions_generated.py`` and
exposed as the :data:`pyhems.REGISTRY` constant.

Usage:
    from pyhems import REGISTRY

    entities = REGISTRY.entities
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum

# ============================================================================
# Entity and Device Definitions
# ============================================================================


class PropertyRole(StrEnum):
    """Role a property plays in the device, independent of any consumer UI.

    Curated per ``(class_code, epc)`` in ``scripts/property_roles.xlsx`` (MRA
    properties) or directly in ``scripts/custom_definitions.yaml`` (custom
    entries), and applied by ``scripts/generate_definitions.py``. Consumers
    (e.g. Home Assistant) map this role to their own UI concepts instead of
    re-deriving it from ``name_en``/``name_ja``.

    Members:
        PRIMARY: State or reading belonging to the device's main function,
          at normal cadence (e.g. cumulative energy, mode, position).
        INSTANTANEOUS: Same as PRIMARY, but a fast-changing measurement
          (e.g. instantaneous power/current/voltage) worth polling at a
          higher frequency than other PRIMARY properties.
        SETTING: Adjusts how the device operates (thresholds, schedules,
          reservations, reset commands).
        STATUS: Fault, maintenance or operating condition reported by the
          device for monitoring purposes.
        SPECIFICATION: Static fact about the hardware that does not change
          during operation (rated values, capacities, equipment type,
          number of significant digits).

    Unreviewed properties default to ``PRIMARY`` (see ``property_roles.xlsx``
    workflow); this is the safe assumption since it corresponds to no
    special treatment by consumers.
    """

    PRIMARY = "primary"
    INSTANTANEOUS = "instantaneous"
    SETTING = "setting"
    STATUS = "status"
    SPECIFICATION = "specification"

    def __repr__(self) -> str:
        """Render as importable source for ``_definitions_generated.py``."""
        return f"{type(self).__name__}.{self.name}"


@dataclass(frozen=True, slots=True)
class EnumValue:
    """A single enum value with EDT, key, and display names.

    Attributes:
        edt: EDT byte value
        key: Identifier key (e.g., "level_1", "on", "off")
        name_en: English display name
        name_ja: Japanese display name
    """

    edt: int
    key: str
    name_en: str
    name_ja: str


@dataclass(frozen=True, slots=True)
class NumericValueEntry:
    """A single EDT byte -> numeric multiplier mapping (MRA ``numericValue`` type).

    Used for unit/coefficient properties (e.g. EPC 0xC2 "Unit for cumulative
    amounts of electric energy") whose EDT byte selects a multiplying factor
    rather than encoding a magnitude directly.

    Attributes:
        edt: EDT byte value
        value: Numeric multiplier represented by this EDT value
    """

    edt: int
    value: float


@dataclass(frozen=True, slots=True)
class EntityDefinition:
    """Definition of an entity to create for a device.

    This is a platform-agnostic definition from MRA data.
    Home Assistant integration infers platform and device_class from these fields.

    Attributes:
        id: Identifier key (e.g., "class_0130_epc_bb")
        epc: ECHONET Lite Property Code
        name_en: English name
        name_ja: Japanese name
        get: Access rule for GET (one of "required", "required_c", "required_o",
          "optional", or "notApplicable")
        set: Access rule for SET (one of "required", "required_c", "required_o",
          "optional", or "notApplicable")
        description_en: English description text from MRA
        description_ja: Japanese description text from MRA
        format: MRA format string for numeric values ("uint8", "int16", etc.)
        unit: MRA unit of measurement ("W", "Celsius", "%RH", etc.)
        minimum: MRA minimum valid value (before scale)
        maximum: MRA maximum valid value (before scale)
        multiple_of: MRA scale factor (e.g., 0.1 for tenths)
        enum_values: Tuple of EnumValue for state options (empty if not applicable)
        byte_offset: Byte position in EDT (0-indexed)
        manufacturer_code: Required manufacturer code (None = all)
        numeric_values: Tuple of NumericValueEntry for MRA ``numericValue``
          properties, or ``None`` when not applicable. Mutually exclusive
          with ``format``/``enum_values``.
        coefficient_epcs: EPCs of sibling properties whose decoded numeric
          value multiplies this property's raw value (MRA ``coefficient``),
          or ``None`` when the value is self-contained.
        role: Curated :class:`PropertyRole`. Defaults to ``PRIMARY`` until
          explicitly reviewed.
    """

    id: str
    epc: int
    name_en: str
    name_ja: str
    get: str
    set: str
    description_en: str | None = None
    description_ja: str | None = None
    format: str | None = None
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    multiple_of: float = 1.0
    enum_values: tuple[EnumValue, ...] = ()
    byte_offset: int = 0
    manufacturer_code: int | None = None
    numeric_values: tuple[NumericValueEntry, ...] | None = None
    coefficient_epcs: tuple[int, ...] | None = None
    role: PropertyRole = PropertyRole.PRIMARY

    def __repr__(self) -> str:
        """Render as ``EntityDefinition(...)`` source, omitting default-valued fields.

        Behaves like the dataclass-generated ``repr()`` except that any field
        whose current value equals its declared default (e.g. ``format=None``,
        ``enum_values=()``, ``multiple_of=1.0``, ``numeric_values=None``) is
        left out entirely, relying on the constructor's own default instead of
        spelling it out. This keeps ``_definitions_generated.py`` compact since
        most entities only populate a handful of the available fields.
        """
        parts = [
            f"{f.name}={getattr(self, f.name)!r}"
            for f in dataclasses.fields(self)
            if f.default is dataclasses.MISSING or getattr(self, f.name) != f.default
        ]
        return f"{type(self).__name__}({', '.join(parts)})"

    def get_binary_values(self) -> tuple[bytes, bytes]:
        """Get ON/OFF byte values for binary entities.

        Binary entities must have exactly one ``key="true"`` and one
        ``key="false"`` enum value. Their order is not significant.

        Returns:
            Tuple of (on_value, off_value) as bytes.

        Raises:
            ValueError: If the enum keys are not exactly ``true`` and ``false``.
        """
        values = {value.key: value.edt for value in self.enum_values}
        if len(self.enum_values) != 2 or set(values) != {"true", "false"}:
            raise ValueError(
                f"Binary entity EPC 0x{self.epc:02X} requires true/false enum keys"
            )
        return bytes([values["true"]]), bytes([values["false"]])

    @property
    def is_binary(self) -> bool:
        """Return whether this entity has normalized boolean enum keys."""
        return len(self.enum_values) == 2 and {
            value.key for value in self.enum_values
        } == {"true", "false"}


# ============================================================================
# Recursive structured value definitions
#
# EntityDefinition above is intentionally flat: one property maps to one
# scalar Python value. Some MRA properties instead describe a variable-length
# list of measurements (``type: array``), optionally wrapping named fields
# (``type: object``) or alternative interpretations (``oneOf``), e.g. class
# 0x0287 EPC 0xBE "Measured instantaneous power consumption list (duplex)".
#
# PropertyValueDefinition recursively models these shapes so a single generic
# decoder (see ``pyhems.codecs.decode_property_value``) can walk any MRA
# property, instead of adding a bespoke Codec per property. CollectionBinding
# then describes how to locate the count/start/items of a paged list result
# within such a decoded value tree.
# ============================================================================


@dataclass(frozen=True, slots=True)
class ScalarDefinition:
    """Leaf numeric/state/raw value within a recursive structured property.

    Mirrors the scalar subset of :class:`EntityDefinition` (format, unit,
    minimum/maximum, multiple_of, enum_values, numeric_values,
    coefficient_epcs) for use as an :class:`ObjectField` value or
    :class:`ArrayDefinition` item, where a full EntityDefinition (with its
    own id/get/set/role) does not apply.

    Attributes:
        size: Byte width of this value within its containing EDT slice.
        format: MRA format string for numeric values, or None.
        unit: MRA unit of measurement, or None.
        minimum: MRA minimum valid raw value (before scale), or None.
        maximum: MRA maximum valid raw value (before scale), or None.
        multiple_of: MRA scale factor.
        enum_values: State options (empty if not applicable).
        numeric_values: MRA numericValue table, or None.
        coefficient_epcs: EPCs of sibling properties whose decoded value
          multiplies this value.
    """

    size: int
    format: str | None = None
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    multiple_of: float = 1.0
    enum_values: tuple[EnumValue, ...] = ()
    numeric_values: tuple[NumericValueEntry, ...] | None = None
    coefficient_epcs: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ObjectField:
    """A single named field of an :class:`ObjectDefinition`.

    Attributes:
        key: Field identifier (MRA ``shortName``).
        name_en: English display name.
        name_ja: Japanese display name.
        value: The field's own (possibly nested) value definition.
    """

    key: str
    name_en: str
    name_ja: str
    value: PropertyValueDefinition


@dataclass(frozen=True, slots=True)
class ObjectDefinition:
    """A fixed set of named fields (MRA ``type: object``).

    Fields decode in order at increasing byte offsets. A trailing
    :class:`ArrayDefinition` field (if any) consumes all remaining bytes of
    the containing EDT rather than a fixed width, matching the MRA
    convention of a variable-length list following fixed header fields (e.g.
    ``startChannel``/``range`` before ``electricEnergy``).
    """

    fields: tuple[ObjectField, ...]


@dataclass(frozen=True, slots=True)
class ArrayDefinition:
    """A variable-length list of items (MRA ``type: array``).

    Attributes:
        item: Value definition shared by every item.
        item_size: Byte width of a single item.
        min_items: MRA minimum item count, or None.
        max_items: MRA maximum item count, or None.
    """

    item: PropertyValueDefinition
    item_size: int
    min_items: int | None = None
    max_items: int | None = None


@dataclass(frozen=True, slots=True)
class OneOfDefinition:
    """An ordered set of alternative interpretations (MRA ``oneOf``).

    Decoding tries each option in order and returns the first successful
    (non-``None``) result. All options are expected to share the same byte
    size (typically a numeric value alongside one or more sentinel states).
    """

    options: tuple[PropertyValueDefinition, ...]


PropertyValueDefinition = (
    ScalarDefinition | ObjectDefinition | ArrayDefinition | OneOfDefinition
)


class CollectionIndex(StrEnum):
    """Semantics of the positions used to correlate collection items.

    Members:
        CHANNEL: Positions are 1-based measurement channel numbers.
    """

    CHANNEL = "channel"

    def __repr__(self) -> str:
        """Render as importable source for ``_definitions_generated.py``."""
        return f"{type(self).__name__}.{self.name}"


@dataclass(frozen=True, slots=True)
class CollectionBinding:
    """Describes how to locate a paged list result within a structured value.

    Bindings are not derived from MRA data (MRA does not document which
    count property backs which list result); they are curated per
    ``class_code`` in ``scripts/custom_definitions.yaml``.

    Attributes:
        result_epc: EPC of the property holding the decoded list (e.g. 0xBE).
        count_epc: EPC of the sibling property declaring the total number of
          items available across all pages (e.g. 0xB8), or None.
        items_path: Path (sequence of :class:`ObjectField` keys) from the
          decoded value root to the list of items.
        start_path: Path to the first index (1-based) covered by this page.
        page_count_path: Path to the declared number of items in this page.
        index_kind: Semantics of item positions.
    """

    result_epc: int
    count_epc: int | None
    items_path: tuple[str, ...]
    start_path: tuple[str, ...]
    page_count_path: tuple[str, ...]
    index_kind: CollectionIndex = CollectionIndex.CHANNEL


@dataclass(frozen=True, slots=True)
class DeviceDefinition:
    """Definition of an ECHONET Lite device class.

    Attributes:
        class_code: ECHONET Lite class code (e.g., 0x0130 for air conditioner)
        name_en: English name
        name_ja: Japanese name
        entities: Tuple of entity definitions for this device class
    """

    class_code: int
    name_en: str
    name_ja: str
    entities: tuple[EntityDefinition, ...]


@dataclass(frozen=True, slots=True)
class ManufacturerDefinition:
    """Definition of an ECHONET Lite manufacturer.

    Attributes:
        name_en: English company name.
        name_ja: Japanese company name.
    """

    name_en: str
    name_ja: str


# ============================================================================
# Definitions Registry
# ============================================================================


@dataclass(frozen=True, slots=True)
class DefinitionsRegistry:
    """Registry of device definitions loaded from JSON.

    This is an immutable data container holding definitions loaded from
    definitions.json (generated from MRA data).

    Use the :data:`pyhems.REGISTRY` constant to access the pre-built registry.

    Attributes:
        version: Definitions format version
        mra_version: MRA data version
        devices: Mapping of class_code to DeviceDefinition
        entities: Mapping of class_code to tuples of EntityDefinition
        manufacturers: Mapping of manufacturer code to ManufacturerDefinition
        structured_values: Mapping of class_code to a mapping of EPC to its
          recursive :data:`PropertyValueDefinition`, populated for
          array/object-shaped properties (see ``ScalarDefinition`` and
          friends). Properties fully represented by ``entities`` are not
          duplicated here.
        collection_bindings: Mapping of class_code to curated
          :class:`CollectionBinding` tuples for its paged list properties.
    """

    version: str
    mra_version: str
    devices: dict[int, DeviceDefinition]
    entities: dict[int, tuple[EntityDefinition, ...]]
    manufacturers: dict[int, ManufacturerDefinition]
    structured_values: dict[int, dict[int, PropertyValueDefinition]] = (
        dataclasses.field(default_factory=dict)
    )
    collection_bindings: dict[int, tuple[CollectionBinding, ...]] = dataclasses.field(
        default_factory=dict
    )
