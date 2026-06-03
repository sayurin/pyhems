"""ECHONET Lite EPC 0x81 (Installation location) decoding.

Per the ECHONET Lite specification (Appendix Table 2-2), the 1-byte
"Installation location" property is encoded as:

- bit 7: free-definition designation bit (1 = user-defined payload, no
  standard meaning).
- bits 6-3 (``LLLL``): installation location code (1..15 are named below;
  see special bytes for the remaining patterns).
- bits 2-0 (``NNN``): location number (0..7; 0 = unspecified).

Special whole-byte values:

- ``0x00``: installation location not specified.
- ``0xFF``: installation location indefinite.
- ``0x01``: position information stored elsewhere (latitude/longitude).
"""

from __future__ import annotations

from dataclasses import dataclass

# Mapping from LLLL code to (translation key, English name, Japanese name).
#
# The translation key is the snake_case identifier used in Home Assistant
# ``strings.json`` ``state`` blocks. The display names follow the wording
# used in the ECHONET Lite specification exactly so consumers can show
# them verbatim (for example as Home Assistant ``suggested_area`` text).
INSTALLATION_LOCATIONS: dict[int, tuple[str, str, str]] = {
    0x1: ("living_room", "Living room", "リビング"),
    0x2: ("dining_room", "Dining room", "ダイニング"),
    0x3: ("kitchen", "Kitchen", "キッチン"),
    0x4: ("bathroom", "Bathroom", "浴室"),
    0x5: ("lavatory", "Lavatory", "トイレ"),
    0x6: ("washroom", "Washroom", "洗面所"),
    0x7: ("passageway", "Passageway", "廊下"),
    0x8: ("room", "Room", "部屋"),
    0x9: ("stairway", "Stairway", "階段"),
    0xA: ("front_door", "Front door", "玄関"),
    0xB: ("storeroom", "Storeroom", "納戸"),
    0xC: ("garden", "Garden", "庭"),
    0xD: ("garage", "Garage", "車庫"),
    0xE: ("balcony", "Balcony", "バルコニー"),
    0xF: ("others", "Others", "その他"),
}


@dataclass(frozen=True, slots=True)
class InstallationLocation:
    """Decoded EPC 0x81 value with a standard ``LLLL`` code.

    Attributes:
        code: ``LLLL`` (1..15).
        key: ``snake_case`` translation key matching ``INSTALLATION_LOCATIONS``.
        name: English display name from the ECHONET Lite specification.
        name_ja: Japanese display name from the ECHONET Lite specification.
        instance: ``NNN`` location number (0..7; 0 = not specified).
    """

    code: int
    key: str
    name: str
    name_ja: str
    instance: int

    @classmethod
    def from_code(cls, code: int, instance: int = 0) -> InstallationLocation:
        """Build an :class:`InstallationLocation` from ``code`` + ``instance``.

        Looks up the standard ``key`` / ``name`` / ``name_ja`` metadata from
        :data:`INSTALLATION_LOCATIONS`. Raises :class:`ValueError` when
        ``code`` is not a standard ``LLLL`` value or ``instance`` is out of
        range (0..7).
        """
        entry = INSTALLATION_LOCATIONS.get(code)
        if entry is None:
            raise ValueError(f"Unknown installation location code: {code}")
        if not 0 <= instance <= 0x07:
            raise ValueError(
                f"Installation location instance must be 0..7, got {instance}"
            )
        key, name, name_ja = entry
        return cls(code=code, key=key, name=name, name_ja=name_ja, instance=instance)


def decode_installation_location(
    raw: bytes | bytearray | int | None,
) -> InstallationLocation | None:
    """Decode EPC 0x81 into an :class:`InstallationLocation`.

    Returns ``None`` when the value does not represent a standard installation
    location: unset (``0x00``), indefinite (``0xFF``), position-information
    marker (``0x01``), free-definition format (bit 7 set), or an unknown
    ``LLLL`` code.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) != 1:
            return None
        byte = raw[0]
    else:
        byte = raw & 0xFF
    if byte in (0x00, 0x01, 0xFF):
        return None
    if byte & 0x80:
        return None
    code = (byte >> 3) & 0x0F
    entry = INSTALLATION_LOCATIONS.get(code)
    if entry is None:
        return None
    key, name, name_ja = entry
    return InstallationLocation(
        code=code, key=key, name=name, name_ja=name_ja, instance=byte & 0x07
    )
