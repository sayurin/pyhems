#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "pydantic",
#   "pyyaml",
# ]
# ///
# ruff: noqa: T201
"""Generate ECHONET Lite definitions from MRA data.

This script:
1. Reads MRA data from the local mra/ directory
2. Parses MRA JSON to extract device and property specifications
3. Loads custom definitions from custom_definitions.yaml
4. Generates definitions.json for runtime entity creation

Run with: uv run scripts/generate_definitions.py

Output files:
- src/pyhems/definitions.json

The generated definitions.json contains:
- common: Entities shared across all device classes (from superClass)
- devices: Device class definitions with entity configurations
  - Each entity has: id, epc, name_en, name_ja, format, unit, minimum, maximum,
    multipleOf, enum_values (fields vary by entity type)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

# ============================================================================
# Constants
# ============================================================================

PYHEMS_DIR = Path(__file__).parent.parent / "src" / "pyhems"
MRA_DIR = Path(__file__).parent.parent / "mra"
CUSTOM_DEFINITIONS_FILE = Path(__file__).parent / "custom_definitions.yaml"


# ============================================================================
# Pydantic Models
# ============================================================================


class EnumValue(BaseModel):
    """A single enum value with EDT, key, and display names."""

    edt: int
    key: str
    name_en: str
    name_ja: str


class MRAProperty(BaseModel):
    """Parsed MRA property data."""

    epc: int
    name_en: str
    name_ja: str
    get: str
    set: str
    data_type: str | None
    mra_format: str | None  # e.g., "uint16", "int8"
    mra_unit: str | None  # e.g., "W", "Wh", "Celsius"
    mra_minimum: float | None
    mra_maximum: float | None
    mra_multiple_of: float | None  # scale factor, e.g., 0.1 for tenths
    enum_values: list[EnumValue]
    has_level_enums: bool = False  # True if any level type was processed


# ============================================================================
# Utility Functions
# ============================================================================


def _parse_hex_int(value: Any) -> int | None:
    """Parse a hex string or int to integer.

    Args:
        value: String like "0x0135" or integer.

    Returns:
        Integer value or None if parsing fails.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)  # auto-detect base (0x for hex)
        except ValueError:
            return None
    return None


# ============================================================================
# MRA Parsing Functions
# ============================================================================


def _parse_mra_property(
    prop_data: dict[str, Any], definitions: dict[str, Any]
) -> MRAProperty:
    """Parse a single MRA property definition.

    Args:
        prop_data: MRA property data
        definitions: MRA definitions for resolving $ref

    Returns:
        Parsed MRAProperty
    """
    # MRA properties always have these required keys: epc, propertyName, accessRule, data
    epc = int(prop_data["epc"], 16)

    name_data = prop_data["propertyName"]
    name_en = name_data["en"]
    name_ja = name_data["ja"]

    # Parse access rules
    access = prop_data["accessRule"]
    get_val = access["get"]
    set_val = access["set"]

    # Parse data specification
    data_spec = prop_data["data"]

    # Collect all data specs to process (for oneOf, process all elements)
    data_specs_to_process: list[dict[str, Any]] = []
    if "oneOf" in data_spec:
        one_of = data_spec["oneOf"]
        if one_of and isinstance(one_of, list):
            data_specs_to_process.extend(one_of)
    else:
        data_specs_to_process.append(data_spec)

    # Resolve $ref and collect all resolved specs
    resolved_specs: list[dict[str, Any]] = []
    for spec in data_specs_to_process:
        if "$ref" in spec:
            ref = spec["$ref"]
            if ref.startswith("#/definitions/"):
                def_name = ref.replace("#/definitions/", "")
                if def_name in definitions:
                    resolved_specs.append(definitions[def_name])
        else:
            resolved_specs.append(spec)

    # Use first spec for primary type inference
    assert resolved_specs, f"No resolved specs for EPC 0x{epc:02X}"
    data_spec = resolved_specs[0]
    data_type: str | None = data_spec["type"]

    # Extract numeric type info from MRA definition
    mra_format: str | None = None
    mra_unit: str | None = None
    mra_minimum: float | None = None
    mra_maximum: float | None = None
    mra_multiple_of: float | None = None

    if data_type == "number":
        mra_format = data_spec.get("format")
        mra_unit = data_spec.get("unit")
        mra_minimum = data_spec.get("minimum")
        mra_maximum = data_spec.get("maximum")
        mra_multiple_of = data_spec.get("multiple") or data_spec.get("multipleOf")

    # Parse enum values from all specs
    enum_values: list[EnumValue] = []
    has_level_enums = False

    for spec in resolved_specs:
        spec_type = spec.get("type")

        # Handle level type
        if spec_type == "level":
            base = int(spec["base"], 16)
            maximum = spec["maximum"]
            if base >= 256 or maximum > 32:
                continue
            for i in range(maximum):
                edt = base + i
                level_num = i + 1
                enum_key = f"level_{level_num}"
                enum_values.append(
                    EnumValue(
                        edt=edt,
                        key=enum_key,
                        name_en=f"Level {level_num}",
                        name_ja=f"レベル{level_num}",
                    )
                )
            data_type = "state"
            has_level_enums = True

        # Handle state type with enum
        elif spec_type == "state":
            for item in spec.get("enum", []):
                edt_str = item["edt"]
                val_name = item["name"]
                if "..." in edt_str:
                    continue
                edt = int(edt_str, 16)
                descriptions = item["descriptions"]
                enum_values.append(
                    EnumValue(
                        edt=edt,
                        key=val_name,
                        name_en=descriptions["en"],
                        name_ja=descriptions["ja"],
                    )
                )

    return MRAProperty(
        epc=epc,
        name_en=name_en,
        name_ja=name_ja,
        get=get_val,
        set=set_val,
        data_type=data_type,
        mra_format=mra_format,
        mra_unit=mra_unit,
        mra_minimum=mra_minimum,
        mra_maximum=mra_maximum,
        mra_multiple_of=mra_multiple_of,
        enum_values=enum_values,
        has_level_enums=has_level_enums,
    )


# ============================================================================
# Entity Building Functions
# ============================================================================


def _build_entity_from_property(
    class_code: int,
    prop: MRAProperty,
) -> dict[str, Any] | None:
    """Build entity dict from MRA property.

    Output schema is platform-agnostic with MRA data only.
    HA integration infers platform and device_class from these fields.
    """
    # Early return if property is not readable
    if prop.get not in ("required", "required_c", "required_o", "optional"):
        return None

    # Determine entity type using inline conditions
    # Sensor: number type with unit
    is_sensor = prop.data_type == "number" and prop.mra_unit is not None
    # State: state type (binary_sensor or select based on enum_values)
    is_state = prop.data_type == "state"

    if not is_sensor and not is_state:
        return None

    # Filter out level-based properties with too many enum values (>16)
    # Pure state enums (e.g., washing machine courses) are kept regardless of count
    if is_state and prop.has_level_enums and len(prop.enum_values) > 16:
        return None

    name_en = prop.name_en
    assert name_en, (
        f"Missing English name for class 0x{class_code:04X} EPC 0x{prop.epc:02X}"
    )

    enum_vals = prop.enum_values
    assert not is_state or enum_vals, (
        f"state entity for class 0x{class_code:04X} EPC 0x{prop.epc:02X} "
        f"({name_en}) has no enum_values"
    )

    # Generate id: class_{class_code}_epc_{epc}
    entity_id = f"class_{class_code:04x}_epc_{prop.epc:02x}"

    entity: dict[str, Any] = {
        "id": entity_id,
        "epc": prop.epc,
        "name_en": name_en,
        "name_ja": prop.name_ja,
        # preserve original access values
        "get": prop.get,
        "set": prop.set,
    }

    # Add sensor-specific MRA fields
    if is_sensor:
        entity["format"] = prop.mra_format
        if prop.mra_unit:
            entity["unit"] = prop.mra_unit
        if prop.mra_minimum is not None:
            entity["minimum"] = prop.mra_minimum
        if prop.mra_maximum is not None:
            entity["maximum"] = prop.mra_maximum
        if prop.mra_multiple_of is not None and prop.mra_multiple_of != 1.0:
            entity["multipleOf"] = prop.mra_multiple_of

    # Include enum_values for state entities (binary/select)
    # For sensors with min/max, filter out out-of-range enum_values (special markers)
    if enum_vals:
        filtered_enums = enum_vals
        if is_sensor and prop.mra_minimum is not None and prop.mra_maximum is not None:
            # Filter out enum_values outside [minimum, maximum] range
            # These are typically special values like "Unmeasurable", "Not measured"
            filtered_enums = [
                ev for ev in enum_vals if prop.mra_minimum <= ev.edt <= prop.mra_maximum
            ]
        if filtered_enums:
            entity["enum_values"] = [ev.model_dump() for ev in filtered_enums]

    # Ensure original access strings are always present
    entity.setdefault("get", prop.get)
    entity.setdefault("set", prop.set)

    return entity


# ============================================================================
# Definition Generation Functions
# ============================================================================


def _load_mra_metadata(mra_path: Path) -> tuple[str, dict[str, Any]]:
    """Load MRA metadata and definitions."""
    with (mra_path / "metaData.json").open(encoding="utf-8") as f:
        metadata = json.load(f)
    mra_version = metadata["metaData"]["dataVersion"]

    with (mra_path / "definitions" / "definitions.json").open(encoding="utf-8") as f:
        defs_data = json.load(f)
    mra_definitions = defs_data["definitions"]

    return mra_version, mra_definitions


def _is_latest_version(prop_data: dict[str, Any]) -> bool:
    """Check if property is valid for the latest MRA version.

    MRA properties have validRelease.to field indicating version validity.
    We only include properties valid for the latest version to avoid duplicates.
    """
    to_value: str = prop_data["validRelease"]["to"]
    return to_value == "latest"


def generate_definitions(mra_path: Path) -> dict[str, Any]:
    """Generate definitions from MRA data."""
    devices_path = mra_path / "devices"
    if not devices_path.exists():
        print(f"Error: devices directory not found at {devices_path}")
        return {}

    mra_version, mra_definitions = _load_mra_metadata(mra_path)

    # Build common entities once (shared across all device classes)
    with (mra_path / "superClass" / "0x0000.json").open(encoding="utf-8") as f:
        superclass = json.load(f)
    common_entities = []
    for prop_data in superclass["elProperties"]:
        if not _is_latest_version(prop_data):
            continue

        prop = _parse_mra_property(prop_data, mra_definitions)
        entity = _build_entity_from_property(0, prop)
        if entity is not None:
            common_entities.append(entity)

    # EPCs already in common section (to avoid duplicates in device-specific entities)
    common_epcs = frozenset(entity["epc"] for entity in common_entities)

    devices: dict[int, dict[str, Any]] = {}

    for device_file in sorted(devices_path.glob("0x*.json")):
        class_code = int(device_file.stem, 16)

        with device_file.open(encoding="utf-8") as f:
            data = json.load(f)

        class_name_data = data["className"]

        entities: list[dict[str, Any]] = []

        for prop_data in data["elProperties"]:
            # Only include properties valid for the latest MRA version
            if not _is_latest_version(prop_data):
                continue

            prop = _parse_mra_property(prop_data, mra_definitions)

            # Skip common EPCs (they are in the common section)
            if prop.epc in common_epcs:
                continue

            entity = _build_entity_from_property(class_code, prop)
            if entity:
                entities.append(entity)

        if entities:
            devices[class_code] = {
                "name_en": class_name_data["en"],
                "name_ja": class_name_data["ja"],
                "entities": entities,
            }

    return {
        "version": "1.0.0",
        "mra_version": mra_version,
        "common": common_entities,
        "devices": devices,
    }


# ============================================================================
# Custom Definitions Loading
# ============================================================================


def _load_custom_definitions(custom_path: Path) -> dict[str, Any]:
    """Load custom definitions from YAML file.

    Args:
        custom_path: Path to custom_definitions.yaml

    Returns:
        Parsed custom definitions or empty dict if not found.
    """
    if not custom_path.exists():
        return {}

    with custom_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data if data else {}


def _merge_custom_definitions(
    definitions: dict[str, Any],
    custom: dict[str, Any],
) -> dict[str, Any]:
    """Merge custom definitions into MRA definitions.

    Args:
        definitions: Generated MRA definitions.
        custom: Custom definitions from YAML.

    Returns:
        Merged definitions.
    """
    devices = definitions.get("devices", {})
    custom_entity_count = 0

    for class_code, class_data in custom.get("devices", {}).items():
        for mfr_code, mfr_data in class_data.get("manufacturers", {}).items():
            mfr_name = mfr_data.get("name", f"Manufacturer {mfr_code:#06x}")
            entities = mfr_data.get("entities", [])

            # Group entities by EPC to generate sequential indices
            epc_counts: dict[int, int] = {}

            for entity in entities:
                entity_result = _build_custom_entity(
                    class_code,
                    mfr_code,
                    entity,
                    epc_counts,
                )
                devices[class_code]["entities"].append(entity_result)
                custom_entity_count += 1

            if entities:
                print(
                    f"  Loaded {len(entities)} entities for {mfr_name} (0x{mfr_code:06X})"
                )

    print(f"  Total custom entities: {custom_entity_count}")

    return definitions


def _build_custom_entity(
    class_code: int,
    mfr_code: int,
    entity: dict[str, Any],
    epc_counts: dict[int, int],
) -> dict[str, Any]:
    """Build a custom entity definition.

    Args:
        class_code: Device class code.
        mfr_code: Manufacturer code.
        entity: Entity definition from YAML.
        epc_counts: Counter for EPCs to generate unique indices.

    Returns:
        Entity definition dict.
    """
    epc: int = entity["epc"]
    enum_values = entity.get("enum_values")

    # Generate unique index for this EPC
    epc_counts.setdefault(epc, 0)
    epc_counts[epc] += 1
    index = epc_counts[epc]

    # Generate id
    entity_id = f"class_{class_code:04x}_custom_{mfr_code:06x}_epc_{epc:02x}_{index}"

    result: dict[str, Any] = {
        "id": entity_id,
        "epc": epc,
        "name_en": entity.get("name_en", ""),
        "name_ja": entity.get("name_ja", ""),
        # preserve access info if provided, else default to notApplicable
        "get": entity.get("get", "notApplicable"),
        "set": entity.get("set", "notApplicable"),
    }

    # State entity: enum_values takes precedence
    if enum_values:
        result["enum_values"] = enum_values
    # Sensor entity: add MRA fields if present
    else:
        if mra_format := entity.get("format"):
            result["format"] = mra_format
        if unit := entity.get("unit"):
            result["unit"] = unit
        if (minimum := entity.get("minimum")) is not None:
            result["minimum"] = minimum
        if (maximum := entity.get("maximum")) is not None:
            result["maximum"] = maximum
        if (multiple_of := entity.get("multipleOf")) is not None and multiple_of != 1.0:
            result["multipleOf"] = multiple_of

    # Vendor-specific fields (flattened)
    result["manufacturer_code"] = mfr_code
    if entity.get("byte_offset") is not None:
        result["byte_offset"] = entity["byte_offset"]

    return result


# ============================================================================
# Main Entry Point
# ============================================================================


def main() -> None:
    """Main entry point."""
    if not MRA_DIR.exists():
        print(f"Error: MRA directory not found at {MRA_DIR}")
        print("Please ensure the mra/ directory exists with MRA data.")
        return

    print(f"Using MRA data from: {MRA_DIR}")
    print("Generating definitions...")
    definitions = generate_definitions(MRA_DIR)

    # Load and merge custom vendor definitions
    if CUSTOM_DEFINITIONS_FILE.exists():
        print(f"\nLoading custom definitions from {CUSTOM_DEFINITIONS_FILE}...")
        custom = _load_custom_definitions(CUSTOM_DEFINITIONS_FILE)
        definitions = _merge_custom_definitions(definitions, custom)

    # Write definitions.json
    definitions_path = PYHEMS_DIR / "definitions.json"
    with definitions_path.open("w", encoding="utf-8") as f:
        json.dump(definitions, f, indent=2, ensure_ascii=False)
    print(f"\nGenerated: {definitions_path}")

    # Print summary
    device_count = len(definitions.get("devices", {}))
    entity_count = sum(
        len(d.get("entities", [])) for d in definitions.get("devices", {}).values()
    )
    print("\nSummary:")
    print(f"  MRA version: {definitions.get('mra_version', 'unknown')}")
    print(f"  Devices: {device_count}")
    print(f"  Entities: {entity_count}")


if __name__ == "__main__":
    main()
