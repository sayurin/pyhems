"""ECHONET Lite protocol frame handling."""

from dataclasses import dataclass, field
from typing import Self


@dataclass(slots=True)
class Property:
    """ECHONET Lite property in a frame."""

    epc: int
    edt: bytes = field(default=b"")

    @property
    def pdc(self) -> int:
        """Return the property data counter (length of EDT)."""
        return len(self.edt)

    def __repr__(self) -> str:
        """Return a string representation of the property."""
        return f"Property(epc=0x{self.epc:02X}, edt={self.edt.hex()})"


@dataclass(slots=True)
class Frame:
    """ECHONET Lite frame structure."""

    seoj: bytes
    deoj: bytes
    esv: int
    tid: int = 0
    properties: list[Property] = field(default_factory=list)

    # ECHONET Lite header constants
    EHD1 = 0x10  # ECHONET Lite
    EHD2 = 0x81  # Format 1

    # Class-level TID generator
    _tid_counter = 0

    def is_response_frame(self) -> bool:
        """Check if frame is a response (success or failure).

        Success responses: 0x70-0x7F
        Failure responses: 0x50-0x5F
        """
        return (0x70 <= self.esv <= 0x7F) or (0x50 <= self.esv <= 0x5F)

    @classmethod
    def next_tid(cls) -> int:
        """Generate next transaction ID."""
        cls._tid_counter = cls._tid_counter + 1
        if cls._tid_counter > 0xFFFF:
            cls._tid_counter = 1
        return cls._tid_counter

    @classmethod
    def decode(cls, data: bytes) -> Self:
        """Decode an ECHONET Lite frame from bytes."""
        if len(data) < 12:
            raise ValueError("Frame too short")

        ehd1, ehd2 = data[0], data[1]
        if ehd1 != cls.EHD1 or ehd2 != cls.EHD2:
            raise ValueError(f"Invalid ECHONET Lite header: {ehd1:#x} {ehd2:#x}")

        tid = int.from_bytes(data[2:4], "big")
        seoj = data[4:7]
        deoj = data[7:10]
        esv = data[10]
        opc = data[11]

        properties: list[Property] = []
        offset = 12
        for _ in range(opc):
            if offset >= len(data):
                raise ValueError("Incomplete property data")
            epc = data[offset]
            pdc = data[offset + 1]
            edt = data[offset + 2 : offset + 2 + pdc]
            properties.append(Property(epc=epc, edt=edt))
            offset += 2 + pdc

        return cls(seoj=seoj, deoj=deoj, esv=esv, tid=tid, properties=properties)

    def encode(self) -> bytes:
        """Encode the frame to bytes."""
        result = bytearray()
        result.append(self.EHD1)
        result.append(self.EHD2)
        result.extend(self.tid.to_bytes(2, "big"))
        result.extend(self.seoj)
        result.extend(self.deoj)
        result.append(self.esv)
        result.append(len(self.properties))

        for prop in self.properties:
            result.append(prop.epc)
            result.append(prop.pdc)
            result.extend(prop.edt)

        return bytes(result)
