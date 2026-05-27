"""Tests for installation_location decoding (EPC 0x81)."""

from __future__ import annotations

import pytest

from pyhems import (
    INSTALLATION_LOCATIONS,
    InstallationLocation,
    decode_installation_location,
)


class TestInstallationLocations:
    """Tests for the INSTALLATION_LOCATIONS table."""

    def test_table_covers_codes_1_through_15(self) -> None:
        """Table has exactly codes 1-15."""
        assert set(INSTALLATION_LOCATIONS) == set(range(1, 16))

    def test_entries_are_key_en_ja_triples(self) -> None:
        """Each entry is a (key, name_en, name_ja) triple with non-empty strings."""
        for code, entry in INSTALLATION_LOCATIONS.items():
            assert len(entry) == 3, code
            key, name_en, name_ja = entry
            assert key, code
            assert name_en, code
            assert name_ja, code
            assert key.replace("_", "").isalnum()

    def test_known_entries(self) -> None:
        """Spot-check a couple of well-known location entries."""
        assert INSTALLATION_LOCATIONS[0x5] == ("lavatory", "Lavatory", "トイレ")
        assert INSTALLATION_LOCATIONS[0xA] == ("front_door", "Front door", "玄関")


class TestDecodeInstallationLocation:
    """Tests for decode_installation_location."""

    def test_none_input(self) -> None:
        """None input returns None."""
        assert decode_installation_location(None) is None

    @pytest.mark.parametrize("raw", [b"", b"\x01\x02"])
    def test_invalid_length(self, raw: bytes) -> None:
        """Byte sequences that are not exactly 1 byte return None."""
        assert decode_installation_location(raw) is None

    @pytest.mark.parametrize("raw", [b"\x00", b"\x01", b"\xff"])
    def test_special_bytes(self, raw: bytes) -> None:
        """0x00 (unset), 0x01 (position info), 0xFF (indefinite) decode to None."""
        assert decode_installation_location(raw) is None

    def test_free_definition_bit(self) -> None:
        """Bit 7 set (free-definition format) decodes to None."""
        assert decode_installation_location(b"\x80") is None
        assert decode_installation_location(b"\xa8") is None

    def test_unknown_llll_code(self) -> None:
        """LLLL = 0 (with bit 7 clear and not 0x00/0x01) decodes to None."""
        # byte = 0b0000_0010 -> LLLL=0, NNN=2: code 0 is not in table.
        assert decode_installation_location(b"\x02") is None

    def test_living_room_no_instance(self) -> None:
        """Byte 0x08 decodes to living_room with instance 0."""
        # byte 0b0000_1000 -> LLLL=1 (living_room), NNN=0
        loc = decode_installation_location(b"\x08")
        assert loc == InstallationLocation(
            code=0x1,
            key="living_room",
            name="Living room",
            name_ja="リビング",
            instance=0,
        )

    def test_kitchen_with_instance(self) -> None:
        """Byte 0x1b decodes to kitchen with instance 3."""
        # byte 0b0001_1011 -> LLLL=3 (kitchen), NNN=3
        loc = decode_installation_location(b"\x1b")
        assert loc is not None
        assert loc.code == 0x3
        assert loc.key == "kitchen"
        assert loc.name == "Kitchen"
        assert loc.name_ja == "キッチン"
        assert loc.instance == 3

    def test_accepts_int_input(self) -> None:
        """Integer input is accepted in addition to bytes."""
        loc = decode_installation_location(0x08)
        assert loc is not None
        assert loc.key == "living_room"
