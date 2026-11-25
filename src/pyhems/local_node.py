"""Fixed local ECHONET Lite node objects exposed by the HEMS client."""

from __future__ import annotations

import hashlib
import uuid

from .const import CONTROLLER_INSTANCE, ESV, NODE_PROFILE_INSTANCE
from .eoj import EOJ
from .frame import Frame, Property

# These values identify the pyhems HEMS endpoint.  The endpoint is deliberately
# static for now: the local objects are protocol bootstrap objects, not a
# second device registry.
_VERSION_INFORMATION = bytes.fromhex("010E0100")
_MANUFACTURER_CODE = bytes.fromhex("FFFFFF")

# The node exposes one controller instance.  Node Profile instance counts and
# lists exclude the Node Profile object itself, while the class count includes
# it.
_INSTANCE_LIST = bytes.fromhex("0105FF01")
_CLASS_LIST = bytes.fromhex("0105FF")

# The startup D5 notification is the only unsolicited notification.  It is
# not a runtime state-change announcement, so it is not represented as a
# changing value in the controller object.
_CONTROLLER_VALUES: dict[int, bytes] = {0x80: b"\x30"}


def _matches_class_instance(target: EOJ, local: EOJ) -> bool:
    """Return whether *target* addresses the local object or all its instances."""
    return target.class_code == local.class_code and target.instance_number in {
        0x00,
        local.instance_number,
    }


class LocalNodeResponder:
    """Respond to the fixed Node Profile and Controller objects."""

    def __init__(self, interface: str = "0.0.0.0") -> None:
        """Initialize fixed values, deriving the node ID from the host identity."""
        host_identity = f"{uuid.getnode():012x}:{interface}".encode()
        unique_id = hashlib.sha256(host_identity).digest()[:13]
        identification_number = b"\xfe" + _MANUFACTURER_CODE + unique_id
        self._node_profile_values = {
            0x80: b"\x30",
            0x82: _VERSION_INFORMATION,
            0x83: identification_number,
            0x8A: _MANUFACTURER_CODE,
            0x9D: bytes.fromhex("0280D5"),
            0x9E: b"\x00",
            0x9F: bytes.fromhex("0B9F9E9D8A808283D3D4D6D7"),
            0xD3: bytes.fromhex("000001"),
            0xD4: bytes.fromhex("0002"),
            0xD6: _INSTANCE_LIST,
            0xD7: _CLASS_LIST,
        }

    def response_seoj(self, deoj: EOJ) -> EOJ | None:
        """Return the concrete local SEOJ addressed by *deoj*."""
        if _matches_class_instance(deoj, NODE_PROFILE_INSTANCE):
            return NODE_PROFILE_INSTANCE
        if _matches_class_instance(deoj, CONTROLLER_INSTANCE):
            return CONTROLLER_INSTANCE
        return None

    def respond(self, frame: Frame) -> Frame | None:
        """Build a response to a GET or INF_REQ addressed to this node."""
        if frame.esv not in {ESV.GET, ESV.INF_REQ}:
            return None

        seoj = self.response_seoj(frame.deoj)
        if seoj is None:
            return None

        values = (
            self._node_profile_values
            if seoj == NODE_PROFILE_INSTANCE
            else _CONTROLLER_VALUES
        )
        properties = [
            Property(epc=property_.epc, edt=values[property_.epc])
            for property_ in frame.properties
            if property_.epc in values
        ]
        if not properties:
            return None

        return Frame(
            tid=frame.tid,
            seoj=seoj,
            deoj=frame.seoj,
            esv=ESV.GET_RES if frame.esv == ESV.GET else ESV.INF,
            properties=properties,
        )

    def startup_announcement(self, tid: int) -> Frame:
        """Build the startup instance-list notification."""
        return Frame(
            tid=tid,
            seoj=NODE_PROFILE_INSTANCE,
            deoj=NODE_PROFILE_INSTANCE,
            esv=ESV.INF,
            properties=[Property(epc=0xD5, edt=_INSTANCE_LIST)],
        )
