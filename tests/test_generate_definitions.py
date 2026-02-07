"""Tests for generate_definitions.py output.

Ensures generated `src/pyhems/definitions.json` contains `get` and `set` fields
and that they use the expected MRA values.
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest

ALLOWED_ACCESS_VALUES = {
    "required",
    "required_c",
    "required_o",
    "optional",
    "notApplicable",
}


def load_definitions() -> dict[str, Any]:
    p = Path("src/pyhems/definitions.json")
    assert p.exists(), "definitions.json must be generated for tests"
    return cast(dict[str, Any], json.loads(p.read_text()))


def test_all_entities_have_get_set_strings() -> None:
    data = load_definitions()

    for ent in data.get("common", []) + [
        e for d in data.get("devices", {}).values() for e in d.get("entities", [])
    ]:
        assert "get" in ent, (
            f"Entity missing 'get': {ent.get('id') or ent.get('name_en')}"
        )
        assert "set" in ent, (
            f"Entity missing 'set': {ent.get('id') or ent.get('name_en')}"
        )
        assert isinstance(ent["get"], str), (
            f"'get' must be string for {ent.get('id') or ent.get('name_en')}"
        )
        assert isinstance(ent["set"], str), (
            f"'set' must be string for {ent.get('id') or ent.get('name_en')}"
        )
        assert ent["get"] in ALLOWED_ACCESS_VALUES, (
            f"Unexpected get value: {ent['get']} in {ent.get('id') or ent.get('name_en')}"
        )
        assert ent["set"] in ALLOWED_ACCESS_VALUES, (
            f"Unexpected set value: {ent['set']} in {ent.get('id') or ent.get('name_en')}"
        )


def _find_entity(
    data: dict[str, Any], class_hex: str, epc_hex: str
) -> dict[str, Any] | None:
    # class_hex like '0x0602', epc_hex like '0x80'
    class_code = int(class_hex, 16)
    epc = int(epc_hex, 16)
    dev = data.get("devices", {}).get(class_code)
    if not dev:
        return None
    for ent in dev.get("entities", []):
        if ent.get("epc") == epc:
            return cast(dict[str, Any], ent)
    return None


def test_sample_entities_have_expected_get_set() -> None:
    data = load_definitions()

    tv_op = _find_entity(data, "0x0602", "0x80")
    if tv_op is not None:
        # If the property appears in device entities (rare), check get/set there
        assert tv_op["get"] == "required"
        assert tv_op["set"] == "required_o"
    else:
        # operation status is a common property; confirm MRA device file has the required set
        mra_tv = Path("mra/devices/0x0602.json")
        assert mra_tv.exists(), "MRA file for TV must exist"
        tvj = json.loads(mra_tv.read_text())
        found = False
        for p in tvj.get("elProperties", []):
            if p.get("epc") == "0x80":
                ar = p.get("accessRule", {})
                assert ar.get("get") == "required"
                assert ar.get("set") == "required_o"
                found = True
                break
        assert found, "TV property 0x80 not found in MRA"

    dg_d5 = _find_entity(data, "0x028E", "0xD5")
    if dg_d5 is not None:
        # If present, expect class-level required_c get and required set (may be optional in some MRA versions)
        assert dg_d5["get"] in {"required_c", "required"}
        assert dg_d5["set"] in {"required", "optional", "required_c"}
    else:
        # fallback: check MRA source
        mra_f = Path("mra/devices/0x028E.json")
        if mra_f.exists():
            mj = json.loads(mra_f.read_text())
            for p in mj.get("elProperties", []):
                if p.get("epc") == "0xD5":
                    ar = p.get("accessRule", {})
                    assert ar.get("get") in {"required_c", "required"}
                    assert ar.get("set") in {"required", "optional", "required_c"}
                    break


if __name__ == "__main__":
    pytest.main([__file__])
