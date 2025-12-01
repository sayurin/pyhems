"""Tests for pyhems utility helpers."""

import pytest

from pyhems.utils import decode_ascii_property, parse_property_map


@pytest.mark.parametrize(
    ("edt", "expected"),
    [
        (b"", None),
        (b"ABC", "ABC"),
        (b"ABC\x00\x00\x00", "ABC"),
        (b"ABC   ", "ABC"),
        (b"ABC\x00 \x00", "ABC"),
        (b" ABC ", " ABC"),
        (b"\x80\x81", None),
    ],
)
def test_decode_ascii_property(edt: bytes, expected: str | None) -> None:
    """Ensure ASCII properties decode correctly with padding removed."""
    assert decode_ascii_property(edt) == expected


@pytest.mark.parametrize(
    ("edt", "expected"),
    [
        (b"", frozenset()),
        (bytes.fromhex("03808182"), frozenset({0x80, 0x81, 0x82})),
        (bytes.fromhex("03ff"), frozenset()),
        (bytes.fromhex("10"), frozenset()),
        (
            bytes.fromhex("10FF00000000000000000000000000000000"),
            frozenset({0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xF0}),
        ),
        (
            bytes.fromhex("1001000000000000000000000000000000"),
            frozenset({0x80}),
        ),
        (
            bytes.fromhex("1003000000000000000000000000000000"),
            frozenset({0x80, 0x90}),
        ),
        (bytes.fromhex("05E0"), frozenset()),
        (bytes.fromhex("10FF"), frozenset()),
        (
            bytes.fromhex("160B010109000000010101030303030303"),
            frozenset(
                {
                    0x80,
                    0x81,
                    0x82,
                    0x83,
                    0x87,
                    0x88,
                    0x89,
                    0x8A,
                    0x8B,
                    0x8C,
                    0x8D,
                    0x8E,
                    0x8F,
                    0x90,
                    0x9A,
                    0x9B,
                    0x9C,
                    0x9D,
                    0x9E,
                    0x9F,
                    0xB0,
                    0xB3,
                }
            ),
        ),
    ],
)
def test_parse_property_map(edt: bytes, expected: frozenset[int]) -> None:
    """Ensure property maps parse to expected EPC sets."""

    assert parse_property_map(edt) == expected
