"""Property-role worksheets cover device-specific MRA definitions."""

from importlib import import_module
from pathlib import Path
from shutil import copyfile

_sync = import_module("scripts.sync_property_roles")
_generator = import_module("scripts.generate_definitions")


def test_scan_includes_device_specific_temporal_overrides() -> None:
    properties = _sync._scan_mra_properties(_sync.MRA_DIR)

    for class_code, epcs in (
        (0x0279, (0x97, 0x98)),
        (0x027D, (0x97, 0x98)),
        (0x02A5, (0x97, 0x98)),
        (0x02A7, (0x98,)),
    ):
        for epc in epcs:
            assert (class_code, epc) in properties
    assert (0, 0x97) in properties
    assert (0x0130, 0x97) not in properties
    assert (0x0279, 0x80) not in properties


def test_worksheet_covers_generated_mra_entities() -> None:
    rows = _sync._load_existing_rows(_sync.PROPERTY_ROLES_FILE)
    build = _generator.generate_definitions(_generator.MRA_DIR)

    for entity in build.common:
        assert (0, entity.epc) in rows
    for class_code, device in build.devices.items():
        for entity in device.entities:
            assert (class_code, entity.epc) in rows


def test_sync_preserves_curated_roles_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "property_roles.xlsx"
    copyfile(_sync.PROPERTY_ROLES_FILE, path)
    before = _sync._load_existing_rows(path)

    _sync.sync_property_roles(_sync.MRA_DIR, path)

    after = _sync._load_existing_rows(path)
    assert after.keys() == before.keys()
    for key, row in before.items():
        assert after[key]["role"] == row["role"]
        assert after[key]["comment"] == row["comment"]
