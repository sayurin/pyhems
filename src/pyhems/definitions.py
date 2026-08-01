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

        For binary entities, determines which EDT values represent ON and OFF states.
        First tries to find enum values with key "true" (ON) and "false" (OFF).
        If not found, uses the first two enum values as ON and OFF respectively.

        Returns:
            Tuple of (on_value, off_value) as bytes.

        Raises:
            ValueError: If fewer than 2 enum values are defined.
        """
        on_value: int | None = None
        off_value: int | None = None

        # First try to find by key
        for ev in self.enum_values:
            if ev.key == "true":
                on_value = ev.edt
            elif ev.key == "false":
                off_value = ev.edt

        # If not found, use first two enum values (first=ON, second=OFF)
        if on_value is None or off_value is None:
            if len(self.enum_values) >= 2:
                on_value = self.enum_values[0].edt
                off_value = self.enum_values[1].edt
            else:
                raise ValueError(
                    f"Binary entity EPC 0x{self.epc:02X} requires at least 2 enum_values"
                )

        return bytes([on_value]), bytes([off_value])


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
    """

    version: str
    mra_version: str
    devices: dict[int, DeviceDefinition]
    entities: dict[int, tuple[EntityDefinition, ...]]
    manufacturers: dict[int, ManufacturerDefinition]
