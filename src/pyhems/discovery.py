"""ECHONET Lite device discovery utilities."""

from .const import (
    EPC_IDENTIFICATION_NUMBER,
    EPC_INSTANCE_LIST,
    EPC_SELF_NODE_INSTANCE_LIST,
)
from .eoj import EOJ
from .frame import Frame


def _extract_discovery_info(frame: Frame) -> tuple[str | None, list[EOJ]]:
    """Extract node_id and instance list from a frame.

    Args:
        frame: The received frame.

    Returns:
        Tuple of (node_id, instances).
        node_id is None if not found.
        instances is a list of EOJ objects.

    """
    node_id: str | None = None
    instances: list[EOJ] = []

    for prop in frame.properties:
        if not prop.edt:
            continue
        if prop.epc == EPC_IDENTIFICATION_NUMBER:
            # Identification number (0x83)
            # Format: 1 byte protocol type (0xFE) + 16 bytes unique ID
            # We use the hex string representation of the whole value
            node_id = prop.edt.hex()
        elif prop.epc in (EPC_INSTANCE_LIST, EPC_SELF_NODE_INSTANCE_LIST):
            # Decode instance list from EDT
            # Format: 1 byte count + (count * 3 bytes for each EOJ)
            count = prop.edt[0]
            for i in range(count):
                offset = 1 + (i * 3)
                if offset + 3 <= len(prop.edt):
                    eoj = EOJ.from_bytes(prop.edt[offset : offset + 3])
                    instances.append(eoj)

    return node_id, instances
