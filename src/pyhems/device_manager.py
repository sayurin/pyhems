"""Device management for ECHONET Lite nodes."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ._definitions_generated import REGISTRY
from .const import (
    CONTROLLER_INSTANCE,
    EPC_GET_PROPERTY_MAP,
    EPC_IDENTIFICATION_NUMBER,
    EPC_INF_PROPERTY_MAP,
    EPC_INSTALLATION_LOCATION,
    EPC_MANUFACTURER_CODE,
    EPC_PRODUCT_CODE,
    EPC_SERIAL_NUMBER,
    EPC_SET_PROPERTY_MAP,
    ESV_GET,
    ESV_INF_REQ,
    ESV_SET_RES,
    ESV_SET_SNA,
)
from .eoj import EOJ
from .frame import Frame, Property
from .installation_location import (
    InstallationLocation,
    decode_installation_location,
)
from .runtime import HemsClient, HemsFrameEvent, HemsInstanceListEvent

_LOGGER = logging.getLogger(__name__)

DeviceCallback = Callable[[str], None]
# Fired with (device_key, tid, esv, epcs_in_frame) for every recognized
# response frame. epcs_in_frame is the set of EPCs actually present in that
# frame, which callers (e.g. PropertyPoller) can compare against the EPCs
# they requested to detect partial responses.
FrameReceivedCallback = Callable[[str, int, int, frozenset[int]], None]


def _parse_property_map(edt: bytes) -> frozenset[int]:
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


def _decode_ascii_property(edt: bytes) -> str | None:
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


def _extract_property_maps(
    properties: Mapping[int, bytes],
) -> tuple[frozenset[int], frozenset[int], frozenset[int]]:
    """Extract and parse Get/Set/Inf property maps from property values."""
    get_epcs = _parse_property_map(properties.get(EPC_GET_PROPERTY_MAP, b""))
    set_epcs = _parse_property_map(properties.get(EPC_SET_PROPERTY_MAP, b""))
    inf_epcs = _parse_property_map(properties.get(EPC_INF_PROPERTY_MAP, b""))
    return get_epcs, set_epcs, inf_epcs


def _extract_node_profile_info(
    properties: Mapping[int, bytes],
) -> tuple[int | None, str | None, str | None]:
    """Extract manufacturer code, product code, and serial number."""
    manufacturer_code: int | None = None
    if (edt := properties.get(EPC_MANUFACTURER_CODE)) and len(edt) >= 3:
        manufacturer_code = int.from_bytes(edt[:3], "big")
    elif (
        (edt := properties.get(EPC_IDENTIFICATION_NUMBER))
        and len(edt) >= 4
        and edt[0] == 0xFE
    ):
        manufacturer_code = int.from_bytes(edt[1:4], "big")

    product_code = _decode_ascii_property(properties.get(EPC_PRODUCT_CODE, b""))
    serial_number = _decode_ascii_property(properties.get(EPC_SERIAL_NUMBER, b""))
    return manufacturer_code, product_code, serial_number


@dataclass(slots=True)
class NodeState:
    """State for a discovered ECHONET Lite node."""

    eoj: EOJ
    properties: dict[int, bytes]
    last_seen: float
    node_id: str
    manufacturer_code: int
    manufacturer_name_en: str | None
    manufacturer_name_ja: str | None
    get_epcs: frozenset[int]
    set_epcs: frozenset[int]
    inf_epcs: frozenset[int]
    poll_epcs: frozenset[int]
    fast_poll_epcs: frozenset[int]
    product_code: str | None
    serial_number: str | None
    class_name_en: str | None = None
    class_name_ja: str | None = None

    @property
    def device_key(self) -> str:
        """Return the unique device key."""
        return f"{self.node_id}-{self.eoj:06x}"

    @property
    def manufacturer_name(self) -> str:
        """Manufacturer display name.

        Returns the English manufacturer name when known, otherwise the
        hexadecimal manufacturer code such as ``"0xABCDEF"``.
        """
        return self.manufacturer_name_en or f"0x{self.manufacturer_code:06X}"

    @property
    def class_name(self) -> str:
        """ECHONET Lite device class display name.

        Returns the English class name when known, otherwise
        ``"ECHONET Lite class 0xXXXX"`` derived from the EOJ class code.
        """
        return self.class_name_en or f"ECHONET Lite class 0x{self.eoj.class_code:04X}"

    @property
    def installation_location(self) -> InstallationLocation | None:
        """Decoded EPC 0x81 (installation location), if available."""
        return decode_installation_location(
            self.properties.get(EPC_INSTALLATION_LOCATION)
        )


class DeviceManager:
    """Manage discovered ECHONET Lite devices.

    Handles device discovery, property tracking, and frame processing.
    This is a protocol-level manager that does not depend on Home Assistant.
    """

    def __init__(
        self,
        client: HemsClient,
        monitored_epcs: Mapping[int, frozenset[int]],
        class_code_filter: frozenset[int] | None = None,
        fast_epcs: Mapping[int, frozenset[int]] | None = None,
    ) -> None:
        """Initialize the device manager.

        Args:
            client: HEMS runtime client for communication.
            monitored_epcs: Mapping of class_code -> EPCs to monitor.
            class_code_filter: If set, only these class codes are accepted.
                If None, all class codes are accepted.
            fast_epcs: Mapping of class_code -> EPCs that should be polled at
                a higher frequency (e.g. instantaneous power). These must be
                a subset of the corresponding ``monitored_epcs`` entry; any
                EPC not present in ``monitored_epcs`` is ignored. Defaults to
                no fast-poll EPCs for any class.
        """
        self._client = client
        self._monitored_epcs = monitored_epcs
        self._class_code_filter = class_code_filter
        self._fast_epcs = fast_epcs or {}

        self.data: dict[str, NodeState] = {}
        self.last_frame_received_at: float | None = None

        self._pending_setups: set[str] = set()
        self._node_profile_info: dict[str, tuple[str | None, str | None]] = {}

        # device_key -> EPC -> number of active subscribers. See
        # :meth:`subscribe_epcs`/:meth:`effective_poll_epcs`.
        self._subscribed_epcs: dict[str, Counter[int]] = {}
        # device_keys for which subscribe_epcs() has been called at least
        # once. Until a device appears here, effective_poll_epcs()/
        # effective_fast_poll_epcs() return the full unfiltered candidate
        # set as a race-safe fallback (e.g. right after platform setup,
        # before any caller has finished registering its subscriptions).
        self._subscription_confirmed: set[str] = set()

        self._on_device_added: list[DeviceCallback] = []
        self._on_device_updated: list[DeviceCallback] = []
        self._on_frame_received: list[FrameReceivedCallback] = []

    def on_device_added(self, callback: DeviceCallback) -> Callable[[], None]:
        """Register a callback for when a new device is added.

        Args:
            callback: Called with device_key when a new device is set up.

        Returns:
            Unsubscribe function.
        """
        self._on_device_added.append(callback)

        def unsub() -> None:
            if callback in self._on_device_added:
                self._on_device_added.remove(callback)

        return unsub

    def on_device_updated(self, callback: DeviceCallback) -> Callable[[], None]:
        """Register a callback for when an existing device's properties change.

        Args:
            callback: Called with device_key when properties are updated.

        Returns:
            Unsubscribe function.
        """
        self._on_device_updated.append(callback)

        def unsub() -> None:
            if callback in self._on_device_updated:
                self._on_device_updated.remove(callback)

        return unsub

    def on_frame_received(self, callback: FrameReceivedCallback) -> Callable[[], None]:
        """Register a callback for when any response frame is processed for a device.

        Unlike :meth:`on_device_updated`, this fires for every recognized
        response frame from a known device, even if no property value
        actually changed (including Set responses). It is primarily used by
        :class:`~pyhems.poller.PropertyPoller` to know when an outstanding
        poll request has been answered, so it can stop waiting and allow the
        next poll to be sent, and to detect partial responses (fewer EPCs in
        the frame than were requested).

        Args:
            callback: Called with (device_key, tid, esv, epcs_in_frame) when a
                response frame is processed. ``epcs_in_frame`` is the set of
                EPCs actually present in that frame (empty for Set responses).

        Returns:
            Unsubscribe function.
        """
        self._on_frame_received.append(callback)

        def unsub() -> None:
            if callback in self._on_frame_received:
                self._on_frame_received.remove(callback)

        return unsub

    def subscribe_epcs(
        self, device_key: str, epcs: frozenset[int]
    ) -> Callable[[], None]:
        """Register a caller's interest in specific EPCs for a device.

        Used by callers that want polling to reflect fine-grained interest
        (e.g. Home Assistant Entity lifecycle: an Entity subscribes to its
        EPC(s) in ``async_added_to_hass()`` and unsubscribes in
        ``async_will_remove_from_hass()``, so a disabled Entity's EPC stops
        being polled). This is a generic reference-counted API: an EPC
        remains "subscribed" as long as at least one caller has an active
        subscription for it, since multiple callers may be interested in the
        same EPC. See :meth:`effective_poll_epcs`.

        Calling this method (even with an empty ``epcs``) marks the device
        as having a confirmed subscriber, which disables the race-safe
        unfiltered fallback described in :meth:`effective_poll_epcs`.

        Args:
            device_key: The device key to subscribe to.
            epcs: EPCs the caller is interested in.

        Returns:
            Unsubscribe function. Idempotent; safe to call more than once.
        """
        counts = self._subscribed_epcs.setdefault(device_key, Counter())
        counts.update(epcs)
        self._subscription_confirmed.add(device_key)

        unsubscribed = False

        def unsub() -> None:
            nonlocal unsubscribed
            if unsubscribed:
                return
            unsubscribed = True
            counts = self._subscribed_epcs.get(device_key)
            if counts is None:
                return
            counts.subtract(epcs)
            for epc in epcs:
                if counts[epc] <= 0:
                    del counts[epc]

        return unsub

    def effective_poll_epcs(self, device_key: str) -> frozenset[int]:
        """Return the normal-tier EPCs actually being polled for a device.

        This is ``NodeState.poll_epcs`` narrowed to the EPCs currently
        subscribed via :meth:`subscribe_epcs` (see that method for the race-
        safe fallback behavior before any subscription has been registered).

        Args:
            device_key: The device key to compute the effective set for.

        Returns:
            The EPCs that should actually be requested, or an empty set if
            the device is unknown.
        """
        node = self.data.get(device_key)
        if node is None:
            return frozenset()
        return self._effective_epcs(device_key, node.poll_epcs)

    def effective_fast_poll_epcs(self, device_key: str) -> frozenset[int]:
        """Return the fast-tier EPCs actually being polled for a device.

        Same as :meth:`effective_poll_epcs`, but narrows
        ``NodeState.fast_poll_epcs`` instead.

        Args:
            device_key: The device key to compute the effective set for.

        Returns:
            The EPCs that should actually be requested, or an empty set if
            the device is unknown.
        """
        node = self.data.get(device_key)
        if node is None:
            return frozenset()
        return self._effective_epcs(device_key, node.fast_poll_epcs)

    def _effective_epcs(
        self, device_key: str, candidate_epcs: frozenset[int]
    ) -> frozenset[int]:
        if device_key not in self._subscription_confirmed:
            return candidate_epcs
        subscribed = frozenset(self._subscribed_epcs.get(device_key, ()))
        return candidate_epcs & subscribed

    def process_frame_event(self, event: HemsFrameEvent) -> bool:
        """Process a received frame and update device state.

        Args:
            event: Frame event from the runtime client.

        Returns:
            True if any device state was updated.
        """
        frame = event.frame
        eoj = event.eoj
        node_id = event.node_id

        if not frame.is_response_frame():
            return False

        device_key = f"{node_id}-{eoj:06x}"
        existing = self.data.get(device_key)

        if existing is None:
            _LOGGER.debug(
                "Ignoring frame for unknown node %s %r (setup handled elsewhere)",
                device_key,
                eoj,
            )
            return False

        self.last_frame_received_at = event.received_at

        received_epcs = frozenset(prop.epc for prop in frame.properties)
        for frame_cb in self._on_frame_received:
            frame_cb(device_key, frame.tid, frame.esv, received_epcs)

        _LOGGER.debug(
            "Received frame for %s (ESV=0x%02X): %r",
            device_key,
            frame.esv,
            frame.properties,
        )

        # Set responses do not carry current property values
        if frame.esv in (ESV_SET_RES, ESV_SET_SNA):
            return False

        updated = False
        for prop in frame.properties:
            current = existing.properties.get(prop.epc)
            if current is None or current != prop.edt:
                existing.properties[prop.epc] = prop.edt
                updated = True

        if updated:
            for updated_cb in self._on_device_updated:
                updated_cb(device_key)

        return updated

    async def process_instance_list_event(
        self, event: HemsInstanceListEvent
    ) -> list[str]:
        """Process instance list and set up newly discovered devices.

        Args:
            event: Instance list event from the runtime client.

        Returns:
            List of device_keys for newly set up devices.
        """
        node_id = event.node_id

        _, product_code, serial_number = _extract_node_profile_info(event.properties)

        if product_code or serial_number:
            self._node_profile_info[node_id] = (product_code, serial_number)

        new_device_keys: list[str] = []
        for eoj in event.instances:
            device_key = f"{node_id}-{eoj:06x}"
            if device_key in self.data or device_key in self._pending_setups:
                continue

            if (
                self._class_code_filter is not None
                and eoj.class_code not in self._class_code_filter
            ):
                _LOGGER.debug(
                    "Skipping device class 0x%04X from node %s (not in filter)",
                    eoj.class_code,
                    node_id,
                )
                continue

            self._pending_setups.add(device_key)
            _LOGGER.debug("Discovered new %r from node %s", eoj, node_id)
            result = await self.setup_device(node_id, eoj)
            if result:
                new_device_keys.append(device_key)

        return new_device_keys

    async def setup_device(self, node_id: str, eoj: EOJ) -> bool:
        """Set up a device by requesting its properties.

        Args:
            node_id: Device node ID.
            eoj: ECHONET object instance.

        Returns:
            True if setup was successful.
        """
        device_key = f"{node_id}-{eoj:06x}"
        try:
            base_epcs = [
                EPC_INF_PROPERTY_MAP,
                EPC_SET_PROPERTY_MAP,
                EPC_GET_PROPERTY_MAP,
                EPC_IDENTIFICATION_NUMBER,
                EPC_MANUFACTURER_CODE,
                EPC_PRODUCT_CODE,
                EPC_SERIAL_NUMBER,
            ]

            initial_epcs = self._monitored_epcs.get(eoj.class_code, frozenset())
            monitored_epcs = initial_epcs - set(base_epcs)
            all_epcs = base_epcs + list(monitored_epcs)

            _LOGGER.debug(
                "Requesting property maps for node %s %r: base=[%s], monitored=[%s]",
                node_id,
                eoj,
                " ".join(f"{epc:02X}" for epc in base_epcs),
                " ".join(f"{epc:02X}" for epc in sorted(monitored_epcs)),
            )

            response_props = await self._client.get(node_id, eoj, all_epcs)
            properties: dict[int, bytes] = {
                prop.epc: prop.edt for prop in response_props if prop.edt
            }

            timestamp = time.monotonic()
            get_epcs, set_epcs, inf_epcs = _extract_property_maps(properties)
            manufacturer_code, product_code, serial_number = _extract_node_profile_info(
                properties
            )

            if manufacturer_code is None:
                _LOGGER.warning(
                    "Device %s has no manufacturer code (EPC 0x8A), skipping",
                    device_key,
                )
                self._pending_setups.discard(device_key)
                return False

            np_info = self._node_profile_info.get(node_id)
            if np_info:
                np_product_code, np_serial_number = np_info
                if not product_code and np_product_code:
                    product_code = np_product_code
                if not serial_number and np_serial_number:
                    serial_number = np_serial_number

            poll_epcs = frozenset((initial_epcs & get_epcs) - inf_epcs)
            fast_poll_epcs = poll_epcs & self._fast_epcs.get(
                eoj.class_code, frozenset()
            )
            poll_epcs -= fast_poll_epcs

            mfr = REGISTRY.manufacturers.get(manufacturer_code)
            manufacturer_name_en = mfr.name_en if mfr else None
            manufacturer_name_ja = mfr.name_ja if mfr else None

            device_def = REGISTRY.devices.get(eoj.class_code)
            class_name_en = device_def.name_en if device_def else None
            class_name_ja = device_def.name_ja if device_def else None

            node = NodeState(
                eoj=eoj,
                properties=properties,
                last_seen=timestamp,
                node_id=node_id,
                get_epcs=get_epcs,
                set_epcs=set_epcs,
                inf_epcs=inf_epcs,
                poll_epcs=poll_epcs,
                fast_poll_epcs=fast_poll_epcs,
                manufacturer_code=manufacturer_code,
                manufacturer_name_en=manufacturer_name_en,
                manufacturer_name_ja=manufacturer_name_ja,
                product_code=product_code,
                serial_number=serial_number,
                class_name_en=class_name_en,
                class_name_ja=class_name_ja,
            )

            self.last_frame_received_at = timestamp
            self.data[device_key] = node

            await self._send_initial_notification(device_key, node)

            self._pending_setups.discard(device_key)
            _LOGGER.info(
                "Created new node %s with %d properties, get=[%s] set=[%s] inf=[%s]",
                device_key,
                len(properties),
                bytes(sorted(get_epcs)).hex(),
                bytes(sorted(set_epcs)).hex(),
                bytes(sorted(inf_epcs)).hex(),
            )

            for cb in self._on_device_added:
                cb(device_key)

            return True

        except Exception:
            self._pending_setups.discard(device_key)
            _LOGGER.exception("Failed to request property maps for %r", eoj)
            return False

    async def _send_initial_notification(
        self, device_key: str, node: NodeState
    ) -> None:
        """Send a one-time INF_REQ for monitored EPCs that support notifications."""
        epcs = set(self._monitored_epcs.get(node.eoj.class_code, frozenset()))
        epcs &= node.inf_epcs

        if not epcs:
            return

        frame = Frame(
            seoj=CONTROLLER_INSTANCE,
            deoj=node.eoj,
            esv=ESV_INF_REQ,
            properties=[Property(epc=epc, edt=b"") for epc in epcs],
        )

        _LOGGER.debug(
            "Sending initial 0x63 notification request to node %s for EPCs: [%s]",
            device_key,
            " ".join(f"{epc:02X}" for epc in sorted(epcs)),
        )

        try:
            sent = await self._client.send(node.node_id, frame)
        except OSError as err:
            _LOGGER.debug(
                "Failed to send initial notifications for node %s: %s",
                device_key,
                err,
            )
        else:
            _LOGGER.debug(
                "Initial 0x63 notification request to node %s sent=%s "
                "TID=0x%04X EPCs=[%s]",
                device_key,
                sent,
                frame.tid,
                " ".join(f"{epc:02X}" for epc in sorted(epcs)),
            )

    async def poll_device(
        self, device_key: str, epcs: frozenset[int] | None = None
    ) -> int | None:
        """Send a GET request for a device's poll EPCs.

        Args:
            device_key: The device key to poll.
            epcs: EPCs to request. Defaults to the device's normal-tier
                ``poll_epcs``; pass ``node.fast_poll_epcs`` explicitly to
                poll the high-frequency tier instead.

        Returns:
            The request TID if the poll request was sent successfully.
        """
        node = self.data.get(device_key)
        if not node:
            return None
        target_epcs = node.poll_epcs if epcs is None else epcs
        if not target_epcs:
            return None

        properties = [Property(epc=epc, edt=b"") for epc in target_epcs]
        frame = Frame(
            seoj=CONTROLLER_INSTANCE,
            deoj=node.eoj,
            esv=ESV_GET,
            properties=properties,
        )
        frame.tid = Frame.next_tid()
        _LOGGER.debug(
            "Sending 0x62 poll to node %s for EPCs: [%s]",
            device_key,
            " ".join(f"{epc:02X}" for epc in sorted(target_epcs)),
        )
        try:
            sent = await self._client.send(node.node_id, frame)
        except OSError as err:
            _LOGGER.debug(
                "Failed to request properties for node %s: %s", device_key, err
            )
            return None
        return frame.tid if sent else None


__all__ = ["DeviceManager", "NodeState"]
