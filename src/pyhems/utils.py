"""Utility helpers for ECHONET Lite processing."""

import logging

_LOGGER = logging.getLogger(__name__)


def parse_property_map(edt: bytes) -> frozenset[int]:
    """Parse an ECHONET Lite property map EDT (0x9D/0x9E/0x9F).

    Property maps have two formats:
    - List format (count <= 15): [count, epc1, epc2, ...]
    - Bitmap format (count >= 16): [count, 16 bytes for EPCs 0x80-0xFF]

    In bitmap format, each bit represents whether an EPC is present.
    The mapping follows the ECHONET Lite specification:
    - Byte index (0-15) = EPC low nibble (0x80, 0x81, ..., 0x8F)
    - Bit index (0-7) = EPC high nibble offset (bit0=0x8x, bit1=0x9x, ..., bit7=0xFx)
    """
    if not edt:
        return frozenset()

    count = edt[0]

    if count <= 15:
        # List format: EPCs are enumerated directly
        if len(edt) < count + 1:
            _LOGGER.debug(
                "Property map list too short: expected %d EPCs, got %d bytes",
                count,
                len(edt) - 1,
            )
            return frozenset()
        return frozenset(edt[1 : count + 1])

    # Bitmap format: 16 bytes representing EPCs 0x80-0xFF
    if len(edt) < 17:
        _LOGGER.debug(
            "Property map bitmap too short: expected 17 bytes, got %d", len(edt)
        )
        return frozenset()

    epcs: set[int] = set()
    for byte_idx in range(16):
        byte_val = edt[1 + byte_idx]
        for bit_idx in range(8):
            if byte_val & (1 << bit_idx):
                # byte_idx = low nibble, bit_idx = high nibble offset
                epc = 0x80 + (bit_idx * 0x10) + byte_idx
                epcs.add(epc)

    return frozenset(epcs)


def decode_ascii_property(edt: bytes) -> str | None:
    """Decode an ASCII string property from EDT.

    Per ECHONET Lite specification, string properties (e.g., product code 0x8C,
    serial number 0x8D) are stored left-justified with NULL or space padding.

    Args:
        edt: Raw EDT bytes.

    Returns:
        Decoded string with padding removed, or None if decoding fails.

    """
    if not edt:
        return None
    try:
        return edt.rstrip(b"\x00 ").decode("ascii")
    except UnicodeDecodeError:
        return None


__all__ = ["decode_ascii_property", "parse_property_map"]
