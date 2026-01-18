"""ECHONET Lite Object (EOJ) representation.

EOJ is a 3-byte identifier for ECHONET Lite objects:
- class_group (1 byte): Device group code (e.g., 0x01 = Air conditioner-related)
- class_type (1 byte): Device class code within the group (e.g., 0x30 = Home air conditioner)
- instance_number (1 byte): Instance number (0x01-0x7F for individual, 0x00 for broadcast)

The combination of class_group and class_type is often referred to as "class_code"
(2 bytes, upper 16 bits of EOJ).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class EOJ:
    r"""ECHONET Lite Object identifier.

    Immutable value object representing a 3-byte EOJ.
    Can be used as dictionary key due to frozen=True.

    Attributes:
        _value: Internal 24-bit integer representation.

    Example:
        >>> eoj = EOJ(0x013001)  # Home air conditioner, instance 1
        >>> eoj.class_code
        0x0130
        >>> eoj.class_group
        0x01
        >>> eoj.class_type
        0x30
        >>> eoj.instance_number
        0x01
        >>> eoj.to_bytes()
        b'\x01\x30\x01'
        >>> f"{eoj:06x}"
        '013001'
    """

    _value: int

    def __post_init__(self) -> None:
        """Validate that value is a 24-bit integer."""
        if not 0 <= self._value <= 0xFFFFFF:
            raise ValueError(f"EOJ must be 24-bit value, got {self._value:#x}")

    @property
    def class_group(self) -> int:
        """Return the class group code (upper byte).

        Example: 0x01 for air conditioner-related devices.
        """
        return (self._value >> 16) & 0xFF

    @property
    def class_type(self) -> int:
        """Return the class type code (middle byte).

        Example: 0x30 for home air conditioner within group 0x01.
        """
        return (self._value >> 8) & 0xFF

    @property
    def class_code(self) -> int:
        """Return the class code (upper 2 bytes: class_group << 8 | class_type).

        This is the device class identifier used for device type discrimination.
        Example: 0x0130 for home air conditioner.
        """
        return self._value >> 8

    @property
    def instance_number(self) -> int:
        """Return the instance number (lower byte).

        Example: 0x01 for the first instance.
        """
        return self._value & 0xFF

    def to_bytes(self) -> bytes:
        """Convert EOJ to 3-byte big-endian bytes for frame encoding."""
        return self._value.to_bytes(3, "big")

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        """Create EOJ from 3-byte big-endian bytes.

        Args:
            data: 3-byte bytes object.

        Returns:
            EOJ instance.

        Raises:
            ValueError: If data is not exactly 3 bytes.
        """
        if len(data) != 3:
            raise ValueError(f"EOJ must be exactly 3 bytes, got {len(data)}")
        return cls(int.from_bytes(data, "big"))

    def __int__(self) -> int:
        """Return the integer representation of the EOJ."""
        return self._value

    def __hash__(self) -> int:
        """Return hash for use as dictionary key."""
        return hash(self._value)

    def __repr__(self) -> str:
        """Return debug representation."""
        return f"EOJ(0x{self._value:06X})"

    def __format__(self, format_spec: str) -> str:
        """Support format specification for the underlying integer value.

        Example:
            >>> f"{eoj:06x}"
            '013001'
            >>> f"0x{eoj:06X}"
            '0x013001'
        """
        return format(self._value, format_spec)
