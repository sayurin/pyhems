"""Tests for protocol frame handling."""

import pytest

from pyhems import Frame, Property
from pyhems.const import (
    EPC_IDENTIFICATION_NUMBER,
    EPC_INSTANCE_LIST,
    EPC_SELF_NODE_INSTANCE_LIST,
    ESV_GET_RES,
)
from pyhems.discovery import extract_discovery_info


class TestProperty:
    """Tests for Property class."""

    def test_property_pdc(self) -> None:
        """Test PDC is computed from EDT length."""
        prop = Property(epc=0x80, edt=b"\x30")
        assert prop.pdc == 1

        prop = Property(epc=0x80, edt=b"\x01\x02\x03")
        assert prop.pdc == 3

        prop = Property(epc=0x80)
        assert prop.pdc == 0


class TestFrame:
    """Tests for Frame class."""

    def test_encode_decode_roundtrip(self) -> None:
        """Test encoding and decoding a frame."""
        frame = Frame(
            tid=0x1234,
            seoj=b"\x05\xff\x01",
            deoj=b"\x0e\xf0\x01",
            esv=0x62,
            properties=[Property(epc=0xD6, edt=b"")],
        )

        encoded = frame.encode()
        decoded = Frame.decode(encoded)

        assert decoded.tid == frame.tid
        assert decoded.seoj == frame.seoj
        assert decoded.deoj == frame.deoj
        assert decoded.esv == frame.esv
        assert len(decoded.properties) == 1
        assert decoded.properties[0].epc == 0xD6

    def test_decode_with_properties(self) -> None:
        """Test decoding frame with property data."""
        # ECHONET Lite frame: Get Response with operation status
        data = bytes(
            [
                0x10,
                0x81,  # EHD1, EHD2
                0x00,
                0x01,  # TID
                0x01,
                0x30,
                0x01,  # SEOJ (air conditioner)
                0x05,
                0xFF,
                0x01,  # DEOJ (controller)
                0x72,  # ESV (Get_Res)
                0x01,  # OPC
                0x80,
                0x01,
                0x30,  # EPC, PDC, EDT (operation status: ON)
            ]
        )

        frame = Frame.decode(data)

        assert frame.tid == 0x0001
        assert frame.seoj == b"\x01\x30\x01"
        assert frame.deoj == b"\x05\xff\x01"
        assert frame.esv == 0x72
        assert len(frame.properties) == 1
        assert frame.properties[0].epc == 0x80
        assert frame.properties[0].edt == b"\x30"

    def test_decode_too_short(self) -> None:
        """Test decoding raises on short data."""
        with pytest.raises(ValueError, match="Frame too short"):
            Frame.decode(b"\x10\x81\x00\x01")

    def test_decode_invalid_header(self) -> None:
        """Test decoding raises on invalid header."""
        data = bytes([0x00, 0x00] + [0] * 10)
        with pytest.raises(ValueError, match="Invalid ECHONET Lite header"):
            Frame.decode(data)


class TestExtractDiscoveryInfo:
    """Tests for extract_discovery_info function."""

    def test_extract_node_id_and_instances(self) -> None:
        """Test extracting both node_id and instances."""
        frame = Frame(
            tid=0x0001,
            seoj=b"\x01\x30\x01",
            deoj=b"\x05\xff\x01",
            esv=ESV_GET_RES,
            properties=[
                Property(
                    epc=EPC_IDENTIFICATION_NUMBER,
                    edt=bytes.fromhex("fe00000601058c53e6fffe513d890ef001"),
                ),
                Property(
                    epc=EPC_SELF_NODE_INSTANCE_LIST,
                    edt=bytes([0x01, 0x01, 0x30, 0x01]),
                ),
            ],
        )

        node_id, instances = extract_discovery_info(frame)
        assert node_id == "fe00000601058c53e6fffe513d890ef001"
        assert instances == [0x013001]

    def test_extract_multiple_instances(self) -> None:
        """Test extracting multiple instances."""
        frame = Frame(
            tid=0x0001,
            seoj=b"\x01\x30\x01",
            deoj=b"\x05\xff\x01",
            esv=ESV_GET_RES,
            properties=[
                Property(
                    epc=EPC_IDENTIFICATION_NUMBER,
                    edt=bytes.fromhex("fe00000601058c53e6fffe513d890ef001"),
                ),
                Property(
                    epc=EPC_INSTANCE_LIST,
                    edt=bytes([0x02, 0x01, 0x30, 0x01, 0x02, 0x79, 0x01]),
                ),
            ],
        )

        node_id, instances = extract_discovery_info(frame)
        assert node_id == "fe00000601058c53e6fffe513d890ef001"
        assert instances == [0x013001, 0x027901]

    def test_extract_missing_node_id(self) -> None:
        """Test extraction when node_id is missing."""
        frame = Frame(
            tid=0x0001,
            seoj=b"\x01\x30\x01",
            deoj=b"\x05\xff\x01",
            esv=ESV_GET_RES,
            properties=[
                Property(
                    epc=EPC_SELF_NODE_INSTANCE_LIST,
                    edt=bytes([0x01, 0x01, 0x30, 0x01]),
                ),
            ],
        )

        node_id, instances = extract_discovery_info(frame)
        assert node_id is None
        assert instances == [0x013001]

    def test_extract_no_instances(self) -> None:
        """Test extraction when no instances are present."""
        frame = Frame(
            tid=0x0001,
            seoj=b"\x01\x30\x01",
            deoj=b"\x05\xff\x01",
            esv=ESV_GET_RES,
            properties=[
                Property(
                    epc=EPC_IDENTIFICATION_NUMBER,
                    edt=bytes.fromhex("fe00000601058c53e6fffe513d890ef001"),
                ),
            ],
        )

        node_id, instances = extract_discovery_info(frame)
        assert node_id == "fe00000601058c53e6fffe513d890ef001"
        assert instances == []
