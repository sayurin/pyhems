#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "pyyaml",
# ]
# ///
# ruff: noqa: T201
"""Generate ECHONET Lite definitions from MRA data.

This script:
1. Reads MRA data from the local mra/ directory
2. Parses MRA JSON to extract device and property specifications
3. Loads custom definitions from custom_definitions.yaml
4. Generates _definitions_generated.py for runtime entity creation

Run with: uv run scripts/generate_definitions.py

Output file:
- src/pyhems/_definitions_generated.py
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

import yaml

# ============================================================================
# Constants
# ============================================================================

PYHEMS_DIR = Path(__file__).parent.parent / "src" / "pyhems"
MRA_DIR = Path(__file__).parent.parent / "mra"
CUSTOM_DEFINITIONS_FILE = Path(__file__).parent / "custom_definitions.yaml"
MANUFACTURER_CODES_FILE = Path(__file__).parent / "manufacturer_codes.yaml"

# ============================================================================
# Load definitions dataclasses from pyhems/definitions.py
#
# Loaded via sys.path so definitions.py can be imported standalone, without
# pulling in the full pyhems package and its runtime dependencies
# (codecs, device_manager, etc.).
# ============================================================================

sys.path.insert(0, str(PYHEMS_DIR))
try:
    import definitions as _defs_mod
finally:
    sys.path.pop(0)

EnumValue: TypeAlias = _defs_mod.EnumValue  # noqa: UP040
EntityDefinition: TypeAlias = _defs_mod.EntityDefinition  # noqa: UP040
DeviceDefinition: TypeAlias = _defs_mod.DeviceDefinition  # noqa: UP040
ManufacturerDefinition: TypeAlias = _defs_mod.ManufacturerDefinition  # noqa: UP040
DefinitionsRegistry: TypeAlias = _defs_mod.DefinitionsRegistry  # noqa: UP040


# ============================================================================
# Internal Build Containers
# ============================================================================


@dataclass
class _DeviceBuild:
    """Mutable container for building device-specific entity lists."""

    name_en: str
    name_ja: str
    entities: list[EntityDefinition]  # device-specific only (excludes common)


@dataclass
class _DefinitionsBuild:
    """Mutable top-level container for the full definitions build."""

    version: str
    mra_version: str
    common: list[EntityDefinition]
    devices: dict[int, _DeviceBuild]
    manufacturers: dict[int, ManufacturerDefinition]


# ============================================================================
# Internal MRA Parsing Model
# ============================================================================


@dataclass
class _MRAProperty:
    """Parsed MRA property data (internal, not exported)."""

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


def _normalize_trailing_number(name: str) -> str:
    """Insert a space before trailing digits if missing.

    e.g. 'Lock setting1' -> 'Lock setting 1'
    Only inserts when at least two letters immediately precede the trailing digits,
    avoiding short codes like 'n1'.
    """
    return re.sub(r"(?<=[a-zA-Z]{2})(\d+)$", r" \1", name)


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
) -> _MRAProperty:
    """Parse a single MRA property definition."""
    # MRA properties always have these required keys: epc, propertyName, accessRule, data
    epc = int(prop_data["epc"], 16)

    name_data = prop_data["propertyName"]
    name_en = _normalize_trailing_number(name_data["en"])
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
                        name_en=_normalize_trailing_number(descriptions["en"]),
                        name_ja=descriptions["ja"],
                    )
                )

    return _MRAProperty(
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
    prop: _MRAProperty,
) -> EntityDefinition | None:
    """Build EntityDefinition directly from an MRA property."""
    is_readable = prop.get in ("required", "required_c", "required_o", "optional")
    is_writable = prop.set in ("required", "required_c", "required_o", "optional")
    assert is_readable or is_writable, (
        f"Property for class 0x{class_code:04X} EPC 0x{prop.epc:02X} "
        "is neither readable nor writable"
    )

    is_sensor = prop.data_type == "number" and prop.mra_unit is not None
    is_state = prop.data_type == "state"

    if not is_sensor and not is_state:
        return None

    # Filter out level-based properties with too many enum values (>16)
    # Pure state enums (e.g., washing machine courses) are kept regardless of count
    if is_state and prop.has_level_enums and len(prop.enum_values) > 16:
        return None

    # Skip entities where different EDT bytes share the same key name.
    # Such enums make encode() non-deterministic and produce an unusable codec.
    if is_state:
        keys = [ev.key for ev in prop.enum_values]
        if len(keys) != len(set(keys)):
            return None

    name_en = prop.name_en
    assert name_en, (
        f"Missing English name for class 0x{class_code:04X} EPC 0x{prop.epc:02X}"
    )

    enum_vals: list[EnumValue] = prop.enum_values
    assert not is_state or enum_vals, (
        f"state entity for class 0x{class_code:04X} EPC 0x{prop.epc:02X} "
        f"({name_en}) has no enum_values"
    )

    # For sensors: filter out out-of-range enum_values (special markers like
    # "Unmeasurable", "Not measured" that sit outside the numeric range)
    if (
        enum_vals
        and is_sensor
        and prop.mra_minimum is not None
        and prop.mra_maximum is not None
    ):
        enum_vals = [
            ev for ev in enum_vals if prop.mra_minimum <= ev.edt <= prop.mra_maximum
        ]

    return EntityDefinition(
        id=f"class_{class_code:04x}_epc_{prop.epc:02x}",
        epc=prop.epc,
        name_en=name_en,
        name_ja=prop.name_ja,
        get=prop.get,
        set=prop.set,
        format=prop.mra_format if is_sensor else None,
        unit=prop.mra_unit if is_sensor else None,
        minimum=prop.mra_minimum if is_sensor else None,
        maximum=prop.mra_maximum if is_sensor else None,
        multiple_of=(
            prop.mra_multiple_of
            if is_sensor and prop.mra_multiple_of is not None
            else 1.0
        ),
        enum_values=tuple(enum_vals),
    )


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


def generate_definitions(mra_path: Path) -> _DefinitionsBuild:
    """Generate definitions from MRA data."""
    devices_path = mra_path / "devices"
    if not devices_path.exists():
        print(f"Error: devices directory not found at {devices_path}")
        return _DefinitionsBuild(
            version="1.0.0",
            mra_version="unknown",
            common=[],
            devices={},
            manufacturers={},
        )

    mra_version, mra_definitions = _load_mra_metadata(mra_path)

    # Build common entities once (shared across all device classes)
    with (mra_path / "superClass" / "0x0000.json").open(encoding="utf-8") as f:
        superclass = json.load(f)
    common: list[EntityDefinition] = []
    for prop_data in superclass["elProperties"]:
        if not _is_latest_version(prop_data):
            continue
        prop = _parse_mra_property(prop_data, mra_definitions)
        entity = _build_entity_from_property(0, prop)
        if entity is not None:
            common.append(entity)

    # EPCs already in common section (to avoid duplicates in device-specific entities)
    common_epcs = frozenset(e.epc for e in common)

    devices: dict[int, _DeviceBuild] = {}

    for device_file in sorted(devices_path.glob("0x*.json")):
        class_code = int(device_file.stem, 16)

        with device_file.open(encoding="utf-8") as f:
            data = json.load(f)

        class_name_data = data["className"]
        entities: list[EntityDefinition] = []

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

        # Register all MRA device classes, even those without device-specific
        # entities, so common entities (e.g., operation status 0x80) are
        # still applied via _load_devices().
        devices[class_code] = _DeviceBuild(
            name_en=class_name_data["en"],
            name_ja=class_name_data["ja"],
            entities=entities,
        )

    manufacturers = _load_manufacturer_codes(MANUFACTURER_CODES_FILE)

    return _DefinitionsBuild(
        version="1.0.0",
        mra_version=mra_version,
        common=common,
        devices=devices,
        manufacturers=manufacturers,
    )


# ============================================================================
# Manufacturer Codes Loading
# ============================================================================


def _load_manufacturer_codes(path: Path) -> dict[int, ManufacturerDefinition]:
    """Load manufacturer codes from YAML.

    Returns a mapping of manufacturer code (int) to ManufacturerDefinition.
    Returns an empty dict if the file does not exist.
    """
    if not path.exists():
        return {}

    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    result: dict[int, ManufacturerDefinition] = {}
    for entry in data.get("manufacturers", []):
        code = entry.get("manufacturer_code")
        if code is None:
            continue
        result[int(code)] = ManufacturerDefinition(
            name_en=entry.get("name_en") or "",
            name_ja=entry.get("name_ja") or "",
        )
    return result


# ============================================================================
# Custom Definitions Loading
# ============================================================================


def _load_custom_definitions(custom_path: Path) -> dict[str, Any]:
    """Load custom definitions from YAML file.

    Args:
        custom_path: Path to custom_definitions.yaml.

    Returns:
        Parsed custom definitions or empty dict if not found.
    """
    if not custom_path.exists():
        return {}

    with custom_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data if data else {}


# YAML field name → EntityDefinition attribute name (where they differ)
_YAML_TO_ATTR: dict[str, str] = {"multipleOf": "multiple_of"}


def _apply_overrides(
    build: _DefinitionsBuild,
    custom: dict[str, Any],
    mra_epcs_by_class: dict[int, frozenset[int]],
) -> None:
    """Apply overrides to existing MRA-generated entities (in-place).

    For each entry whose EPC already exists in the MRA definitions,
    patch the matching EntityDefinition using dataclasses.replace().
    ``enum_values`` uses key-based merge (matched by ``key``, updating
    ``name_en``/``name_ja``).  When the override covers ALL enum keys,
    the enum_values are also reordered to match the override list order;
    partial overrides leave the original MRA order unchanged.
    """
    override_count = 0

    for class_code, entries in custom.get("devices", {}).items():
        if class_code not in build.devices:
            continue

        device_build = build.devices[class_code]
        mra_epcs = mra_epcs_by_class.get(class_code, frozenset())

        for entry in entries:
            epc: int = entry["epc"]
            if epc not in mra_epcs:
                continue  # Not an override — handled by _merge_custom_definitions

            manufacturer_code = entry.get("manufacturer_code")

            # Collect indices of matching entities
            matching_idx = [
                i
                for i, e in enumerate(device_build.entities)
                if e.epc == epc
                and (
                    manufacturer_code is None
                    or e.manufacturer_code == manufacturer_code
                )
            ]
            if not matching_idx:
                print(
                    f"  Warning: override target EPC 0x{epc:02X} not found "
                    f"in class 0x{class_code:04X}"
                )
                continue

            match_keys = {"epc", "manufacturer_code"}

            for idx in matching_idx:
                entity = device_build.entities[idx]
                changes: dict[str, Any] = {}

                for field, value in entry.items():
                    if field in match_keys:
                        continue
                    if field == "enum_values":
                        # Key-based merge for enum_values.
                        # Reordering is applied only when the override covers
                        # ALL enum keys (full coverage), so that partial
                        # overrides (label-only fixes) do not disturb the
                        # original MRA order.
                        current_evs = list(entity.enum_values)
                        updated: dict[str, EnumValue] = {}
                        override_order: list[str] = []
                        for ev_override in value:
                            key = ev_override["key"]
                            matched = [ev for ev in current_evs if ev.key == key]
                            if not matched:
                                print(
                                    f"  Warning: override enum key '{key}' "
                                    f"not found in EPC 0x{epc:02X} of "
                                    f"class 0x{class_code:04X}"
                                )
                                continue
                            for ev in matched:
                                updated[key] = dataclasses.replace(
                                    ev,
                                    name_en=ev_override.get("name_en", ev.name_en),
                                    name_ja=ev_override.get("name_ja", ev.name_ja),
                                )
                                override_count += 1
                            override_order.append(key)

                        new_evs = [updated.get(ev.key, ev) for ev in current_evs]

                        # Only reorder when the override fully covers every key
                        if override_order and len(override_order) == len(current_evs):
                            key_index = {k: i for i, k in enumerate(override_order)}
                            new_evs.sort(key=lambda ev: key_index[ev.key])

                        changes["enum_values"] = tuple(new_evs)
                    else:
                        attr = _YAML_TO_ATTR.get(field, field)
                        changes[attr] = value
                        override_count += 1

                if changes:
                    device_build.entities[idx] = dataclasses.replace(entity, **changes)

    if override_count:
        print(f"  Applied {override_count} override(s)")


def _merge_custom_definitions(
    build: _DefinitionsBuild,
    custom: dict[str, Any],
    mra_epcs_by_class: dict[int, frozenset[int]],
) -> None:
    """Merge new custom entities into definitions (in-place).

    For each entry whose EPC does NOT exist in the MRA definitions,
    create a new EntityDefinition and append it.
    """
    custom_entity_count = 0

    for class_code, entries in custom.get("devices", {}).items():
        if class_code not in build.devices:
            print(f"  Warning: custom target class 0x{class_code:04X} not found")
            continue

        mra_epcs = mra_epcs_by_class.get(class_code, frozenset())

        for entry in entries:
            epc: int = entry["epc"]
            if epc in mra_epcs:
                continue  # Override — handled by _apply_overrides

            entity = _build_custom_entity(class_code, entry)
            build.devices[class_code].entities.append(entity)
            custom_entity_count += 1

    if custom_entity_count:
        print(f"  Total custom entities: {custom_entity_count}")


def _build_custom_entity(
    class_code: int,
    entry: dict[str, Any],
) -> EntityDefinition:
    """Build a custom EntityDefinition from a YAML entry."""
    epc: int = entry["epc"]
    mfr_code: int | None = entry.get("manufacturer_code")
    byte_offset: int = entry.get("byte_offset", 0)
    enum_values_raw = entry.get("enum_values")

    entity_id = (
        f"class_{class_code:04x}_epc_{epc:02x}_custom_{mfr_code:06x}_{byte_offset:02x}"
        if mfr_code is not None
        else f"class_{class_code:04x}_epc_{epc:02x}_custom_{byte_offset:02x}"
    )
    enum_tuple = (
        tuple(
            EnumValue(
                edt=ev["edt"],
                key=ev["key"],
                name_en=ev.get("name_en", ""),
                name_ja=ev.get("name_ja", ""),
            )
            for ev in enum_values_raw
        )
        if enum_values_raw
        else ()
    )

    return EntityDefinition(
        id=entity_id,
        epc=epc,
        name_en=entry.get("name_en", ""),
        name_ja=entry.get("name_ja", ""),
        get=entry.get("get", "notApplicable"),
        set=entry.get("set", "notApplicable"),
        format=entry.get("format") if not enum_values_raw else None,
        unit=entry.get("unit") if not enum_values_raw else None,
        minimum=entry.get("minimum") if not enum_values_raw else None,
        maximum=entry.get("maximum") if not enum_values_raw else None,
        multiple_of=(entry.get("multipleOf", 1.0) if not enum_values_raw else 1.0),
        enum_values=enum_tuple,
        byte_offset=entry.get("byte_offset", 0),
        manufacturer_code=mfr_code,
    )


# ============================================================================
# Code Generation Functions
# ============================================================================


def _validate_entity(entity: EntityDefinition, class_code: int) -> None:
    """Validate an entity definition at build time."""
    assert entity.enum_values or entity.format, (
        f"Entity EPC 0x{entity.epc:02X} for class 0x{class_code:04X} missing format"
    )
    assert (
        not entity.enum_values
        or len(entity.enum_values) != 1
        or entity.get == "notApplicable"
        or entity.format is not None
    ), (
        f"Entity EPC 0x{entity.epc:02X} for class 0x{class_code:04X}"
        " has only 1 enum_value"
    )


def _generate_python_source(build: _DefinitionsBuild) -> str:
    """Render definitions as importable Python source.

    Produces a module that builds the DefinitionsRegistry from dataclass
    literals (rendered via repr), so the data is referenced directly as code
    instead of being parsed from JSON at runtime. Common entities are emitted
    once as ``_COMMON`` and shared by every device class.
    """
    # Build-time validation
    for class_code, device_build in build.devices.items():
        for entity in build.common + device_build.entities:
            _validate_entity(entity, class_code)

    lines: list[str] = [
        "# ruff: noqa",
        '"""Auto-generated ECHONET Lite definitions.',
        "",
        "DO NOT EDIT. Generated by scripts/generate_definitions.py.",
        '"""',
        "",
        "from .definitions import (",
        "    DefinitionsRegistry,",
        "    DeviceDefinition,",
        "    EntityDefinition,",
        "    EnumValue,",
        "    ManufacturerDefinition,",
        ")",
        "",
        "_COMMON: tuple[EntityDefinition, ...] = (",
    ]
    lines.extend(f"    {entity!r}," for entity in build.common)
    lines.append(")")
    lines.append("")
    lines.append("DEVICES: dict[int, DeviceDefinition] = {")
    for class_code in sorted(build.devices):
        device_build = build.devices[class_code]
        lines.append(f"    {class_code}: DeviceDefinition(")
        lines.append(f"        class_code={class_code},")
        lines.append(f"        name_en={device_build.name_en!r},")
        lines.append(f"        name_ja={device_build.name_ja!r},")
        if device_build.entities:
            lines.append("        entities=_COMMON + (")
            lines.extend(f"            {entity!r}," for entity in device_build.entities)
            lines.append("        ),")
        else:
            lines.append("        entities=_COMMON,")
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    lines.append("MANUFACTURERS: dict[int, ManufacturerDefinition] = {")
    lines.extend(
        f"    {code}: {build.manufacturers[code]!r},"
        for code in sorted(build.manufacturers)
    )
    lines.append("}")
    lines.append("")
    lines.extend(
        [
            "REGISTRY = DefinitionsRegistry(",
            f"    version={build.version!r},",
            f"    mra_version={build.mra_version!r},",
            "    devices=DEVICES,",
            "    entities={cc: d.entities for cc, d in DEVICES.items()},",
            "    manufacturers=MANUFACTURERS,",
            ")",
            "",
        ]
    )
    return "\n".join(lines)


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
    build = generate_definitions(MRA_DIR)

    # Load and merge custom vendor definitions
    if CUSTOM_DEFINITIONS_FILE.exists():
        print(f"\nLoading custom definitions from {CUSTOM_DEFINITIONS_FILE}...")
        custom = _load_custom_definitions(CUSTOM_DEFINITIONS_FILE)
        # Snapshot MRA EPCs before custom processing so ADD/UPDATE detection
        # is based on the original MRA data, not entities added by merging.
        mra_epcs_by_class = {
            class_code: frozenset(e.epc for e in device_build.entities)
            for class_code, device_build in build.devices.items()
        }
        _merge_custom_definitions(build, custom, mra_epcs_by_class)  # ADD first
        _apply_overrides(build, custom, mra_epcs_by_class)  # then UPDATE

    generated_source = _generate_python_source(build)
    generated_path = PYHEMS_DIR / "_definitions_generated.py"
    with generated_path.open("w", encoding="utf-8") as f:
        f.write(generated_source)
    print(f"\nGenerated: {generated_path}")

    device_count = len(build.devices)
    entity_count = sum(len(db.entities) for db in build.devices.values())
    manufacturer_count = len(build.manufacturers)
    print("\nSummary:")
    print(f"  MRA version: {build.mra_version}")
    print(f"  Devices: {device_count}")
    print(f"  Entities: {entity_count}")
    print(f"  Manufacturers: {manufacturer_count}")


if __name__ == "__main__":
    main()
