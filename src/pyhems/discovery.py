"""ECHONET Lite device discovery utilities."""

from .const import (
    EPC_IDENTIFICATION_NUMBER,
    EPC_INSTANCE_LIST,
    EPC_SELF_NODE_INSTANCE_LIST,
)
from .frame import Frame


def extract_discovery_info(frame: Frame) -> tuple[str | None, list[int]]:
    """Extract node_id and instance list from a frame.

    Args:
        frame: The received frame.

    Returns:
        Tuple of (node_id, instances).
        node_id is None if not found.
        instances is a list of instance EOJs.

    """
    node_id: str | None = None
    instances: list[int] = []

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
                    eoj = int.from_bytes(prop.edt[offset : offset + 3], "big")
                    instances.append(eoj)

    return node_id, instances
