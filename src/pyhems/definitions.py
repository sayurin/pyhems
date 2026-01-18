"""ECHONET Lite definitions for entity creation.

This module provides:
- EntityDefinition for entity configuration
- DeviceDefinition for device class configuration
- DefinitionsRegistry for managing all definitions
- Decoder factory functions for creating EDT decoders

Usage:
    from pyhems import load_definitions_registry

    registry = load_definitions_registry()
    entities = registry.get_entities("sensor")
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class DefinitionsLoadError(Exception):
    """Raised when definitions cannot be loaded."""


# ============================================================================
# Decoder Factory Functions
# ============================================================================

# MRA format string -> (signed, byte_count)
_FORMAT_INFO: dict[str, tuple[bool, int]] = {
    "uint8": (False, 1),
    "int8": (True, 1),
    "uint16": (False, 2),
    "int16": (True, 2),
    "uint32": (False, 4),
    "int32": (True, 4),
}


def create_numeric_decoder(
    mra_format: str,
    minimum: float | None = None,
    maximum: float | None = None,
    scale: float = 1.0,
    byte_offset: int = 0,
) -> Callable[[bytes], float | int | None]:
    """Create a numeric decoder function for EDT data.

    Args:
        mra_format: MRA format string (e.g., "uint8", "int16", "uint32")
        minimum: Minimum valid value (before scale). Values below are invalid.
        maximum: Maximum valid value (before scale). Values above are invalid.
        scale: Scale factor to apply (default 1.0)
        byte_offset: Byte offset within EDT data (default 0)

    Returns:
        A function that decodes EDT bytes to a numeric value.
        Returns None for values outside [minimum, maximum] range.

    Raises:
        ValueError: If mra_format is not recognized.
    """
    format_info = _FORMAT_INFO.get(mra_format)
    if not format_info:
        raise ValueError(f"Unknown MRA format: {mra_format}")

    signed, byte_count = format_info
    required_len = byte_offset + byte_count

    def numeric_decoder(state: bytes) -> float | int | None:
        if not state or len(state) < required_len:
            return None
        raw = int.from_bytes(
            state[byte_offset : byte_offset + byte_count], "big", signed=signed
        )
        # Check range (invalid values return None)
        if minimum is not None and raw < minimum:
            return None
        if maximum is not None and raw > maximum:
            return None
        return raw if scale == 1.0 else raw * scale

    return numeric_decoder


def create_binary_decoder(on_value: bytes) -> Callable[[bytes], bool | None]:
    r"""Create a binary decoder function for EDT data.

    Args:
        on_value: Bytes representing ON state (e.g., b"\x30")

    Returns:
        A function that decodes EDT bytes to a boolean.
    """

    def _binary_decoder(state: bytes) -> bool | None:
        return state == on_value if state else None

    return _binary_decoder


def create_enum_decoder() -> Callable[[bytes], int | None]:
    """Create an enum decoder function for EDT data.

    Returns:
        A function that decodes EDT bytes to an integer enum value.
    """

    def _enum_decoder(state: bytes) -> int | None:
        return state[0] if state else None

    return _enum_decoder


# ============================================================================
# Entity and Device Definitions
# ============================================================================


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
class EntityDefinition:
    """Definition of an entity to create for a device.

    This is a platform-agnostic definition from MRA data.
    Home Assistant integration infers platform and device_class from these fields.

    Attributes:
        id: Identifier key (e.g., "class_0130_epc_bb")
        epc: ECHONET Lite Property Code
        name_en: English name
        name_ja: Japanese name
        format: MRA format string for numeric values ("uint8", "int16", etc.)
        unit: MRA unit of measurement ("W", "Celsius", "%RH", etc.)
        minimum: MRA minimum valid value (before scale)
        maximum: MRA maximum valid value (before scale)
        multiple_of: MRA scale factor (e.g., 0.1 for tenths)
        enum_values: Tuple of EnumValue for state options (empty if not applicable)
        byte_offset: Byte position in EDT (0-indexed)
        manufacturer_code: Required manufacturer code (None = all)
    """

    id: str
    epc: int
    name_en: str
    name_ja: str
    format: str | None = None
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    multiple_of: float = 1.0
    enum_values: tuple[EnumValue, ...] = ()
    byte_offset: int = 0
    manufacturer_code: int | None = None

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


# ============================================================================
# Definitions Registry
# ============================================================================


@dataclass(frozen=True, slots=True)
class DefinitionsRegistry:
    """Registry of device definitions loaded from JSON.

    This is an immutable data container holding definitions loaded from
    definitions.json (generated from MRA data).

    Use load_definitions_registry() or async_load_definitions_registry() to create.

    Attributes:
        version: Definitions format version
        mra_version: MRA data version
        entities: Mapping of class_code to tuples of EntityDefinition
    """

    version: str
    mra_version: str
    entities: dict[int, tuple[EntityDefinition, ...]]


# ============================================================================
# Definition Loading Functions
# ============================================================================

# Default definitions file path (in package directory)
DEFINITIONS_FILE = Path(__file__).parent / "definitions.json"


def _get_definitions_data(definitions_file: Path | None = None) -> dict[str, Any]:
    """Get raw definitions data from definitions.json.

    Args:
        definitions_file: Path to definitions.json. If None, uses bundled file.

    Returns:
        Parsed definitions.json dictionary containing:
        - version: Definitions format version
        - mra_version: MRA specification version
        - devices: Device definitions with entities

    Raises:
        DefinitionsLoadError: If the file cannot be loaded.
    """
    path = definitions_file or DEFINITIONS_FILE
    if not path.exists():
        raise DefinitionsLoadError(f"Definitions file not found: {path}")

    try:
        with path.open(encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result
    except (json.JSONDecodeError, OSError) as ex:
        raise DefinitionsLoadError(f"Failed to load definitions: {ex}") from ex


def _parse_entity(entity_data: dict[str, Any]) -> EntityDefinition:
    """Parse a single entity definition.

    Args:
        entity_data: Entity definition dictionary.

    Returns:
        EntityDefinition.
    """
    # EPC is always int in normalized definitions.json
    epc: int = entity_data["epc"]

    # Parse enum values
    enum_values = tuple(
        EnumValue(
            edt=item["edt"],
            key=item["key"],
            name_en=item["name_en"],
            name_ja=item["name_ja"],
        )
        for item in entity_data.get("enum_values", [])
    )

    # Get MRA fields (None if not present)
    mra_format = entity_data.get("format")
    unit = entity_data.get("unit")
    minimum = entity_data.get("minimum")
    maximum = entity_data.get("maximum")
    multiple_of = entity_data.get("multipleOf", 1.0)

    # Must have either format (sensor) or enum_values (binary/select)
    assert mra_format or enum_values, (
        f"Entity EPC 0x{epc:02X} has neither format nor enum_values"
    )

    # Vendor-specific fields (flattened)
    manufacturer_code = entity_data.get("manufacturer_code")
    byte_offset = entity_data.get("byte_offset", 0)

    return EntityDefinition(
        id=entity_data["id"],
        epc=epc,
        name_en=entity_data["name_en"],
        name_ja=entity_data["name_ja"],
        format=mra_format,
        unit=unit,
        minimum=minimum,
        maximum=maximum,
        multiple_of=multiple_of,
        enum_values=enum_values,
        byte_offset=byte_offset,
        manufacturer_code=manufacturer_code,
    )


def _load_devices(
    devices_data: dict[str, Any],
    common_data: list[dict[str, Any]],
) -> dict[int, DeviceDefinition]:
    """Load device definitions from parsed JSON data.

    Args:
        devices_data: Dictionary of class_code to device data.
        common_data: List of common entity definitions.

    Returns:
        Dictionary of class_code to DeviceDefinition.
    """
    common_entities = [_parse_entity(entity_data) for entity_data in common_data]
    devices: dict[int, DeviceDefinition] = {}

    for class_code_key, device_data in devices_data.items():
        try:
            class_code = int(class_code_key)
        except ValueError:
            continue

        device_entities = [
            _parse_entity(entity_data)
            for entity_data in device_data.get("entities", [])
        ]

        devices[class_code] = DeviceDefinition(
            class_code=class_code,
            name_en=device_data["name_en"],
            name_ja=device_data["name_ja"],
            entities=tuple(common_entities + device_entities),
        )

    return devices


def _validate_entity(entity: EntityDefinition, class_code: int) -> bool:
    """Validate that an entity definition is complete.

    Args:
        entity: Entity definition to validate.
        class_code: Device class code for logging.

    Returns:
        True if valid, False otherwise.
    """
    # Sensor entities (no enum_values) need format
    if not entity.enum_values and not entity.format:
        _LOGGER.warning(
            "Entity EPC 0x%02X for class 0x%04X missing format",
            entity.epc,
            class_code,
        )
        return False

    # Binary entities (2 enum_values) need at least 2 values
    if entity.enum_values and len(entity.enum_values) == 1:
        _LOGGER.warning(
            "Entity EPC 0x%02X for class 0x%04X has only 1 enum_value",
            entity.epc,
            class_code,
        )
        return False

    return True


def _build_entities(
    devices: dict[int, DeviceDefinition],
) -> dict[int, tuple[EntityDefinition, ...]]:
    """Build entities from loaded definitions.

    Args:
        devices: Device definitions.

    Returns:
        Mapping of class_code to tuples of EntityDefinition.
    """
    return {
        cc: tuple(entity for entity in device.entities if _validate_entity(entity, cc))
        for cc, device in devices.items()
    }


def load_definitions_registry(
    definitions_file: Path | None = None,
) -> DefinitionsRegistry:
    """Load and create a DefinitionsRegistry.

    Args:
        definitions_file: Path to definitions.json. If None, uses bundled file.

    Returns:
        Populated DefinitionsRegistry instance.

    Raises:
        DefinitionsLoadError: If definitions.json cannot be loaded.
    """
    data = _get_definitions_data(definitions_file)

    version = data.get("version", "unknown")
    mra_version = data.get("mra_version", "unknown")
    devices = _load_devices(data.get("devices", {}), data.get("common", []))

    _LOGGER.debug(
        "Loaded %d device definitions (MRA version: %s)", len(devices), mra_version
    )

    entities = _build_entities(devices)

    return DefinitionsRegistry(
        version=version,
        mra_version=mra_version,
        entities=entities,
    )
