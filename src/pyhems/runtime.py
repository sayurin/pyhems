"""HEMS runtime client for ECHONET Lite communication."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from bidict import bidict

from .const import (
    CONTROLLER_INSTANCE,
    DISCOVERY_DEFAULT_EPCS,
    ECHONET_MULTICAST,
    ESV_GET,
    ESV_GET_RES,
    ESV_GET_SNA,
    GET_MAX_RETRIES,
    GET_TIMEOUT,
    NODE_PROFILE_CLASS,
    NODE_PROFILE_INSTANCE,
)
from .discovery import extract_discovery_info
from .frame import Frame, Property
from .transport import EchonetLiteProtocol, create_multicast_socket

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeEvent:
    """Base class for runtime events."""

    received_at: float


@dataclass(slots=True)
class HemsFrameEvent(RuntimeEvent):
    """Event containing a received ECHONET Lite frame."""

    frame: Frame
    node_id: str
    eoj: int


@dataclass(slots=True)
class HemsInstanceListEvent(RuntimeEvent):
    """Event containing discovered instances from a device.

    Attributes:
        instances: List of EOJs (as integers) discovered on the device.
        node_id: Device node ID (hex string from EPC 0x83).
        properties: All properties from the node profile response.
            Key is EPC, value is EDT bytes.

    """

    instances: list[int]
    node_id: str
    properties: dict[int, bytes]


@dataclass(slots=True)
class HemsErrorEvent(RuntimeEvent):
    """Event indicating a runtime error."""

    error: Exception


EventCallback = Callable[[RuntimeEvent], None]


class HemsClient:
    """Runtime client for ECHONET Lite HEMS communication."""

    def __init__(
        self,
        interface: str = "0.0.0.0",
        poll_interval: float = 60.0,
        extra_epcs: list[int] | None = None,
    ) -> None:
        """Initialize the HEMS client.

        Args:
            interface: Network interface IP to bind to.
            poll_interval: Interval for polling devices (seconds).
            extra_epcs: Additional EPCs to request from node profile during
                discovery. These will be included in HemsInstanceListEvent.properties.

        """
        self._interface = interface
        self._poll_interval = poll_interval
        # Combine default EPCs with extra EPCs, preserving order and avoiding duplicates
        self._discovery_epcs = list(
            dict.fromkeys(list(DISCOVERY_DEFAULT_EPCS) + (extra_epcs or []))
        )
        self._protocol: EchonetLiteProtocol | None = None
        self._callbacks: list[EventCallback] = []
        # address <-> node_id (hex) bidirectional mapping
        self._device_addresses: bidict[str, str] = bidict()
        # Queue to store frames from unknown devices (frame, eoj, received_at)
        self._pending_frames: dict[str, list[tuple[Frame, int, float]]] = {}
        # Background tasks that need to be kept alive
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._poll_task: asyncio.Task[None] | None = None
        # Pending Get requests: tid -> (address, deoj, requested_epcs, future)
        self._pending_gets: dict[
            int, tuple[str, int, list[int], asyncio.Future[list[Property]]]
        ] = {}

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        """Subscribe to runtime events.

        Args:
            callback: Function to call when events occur.

        Returns:
            Unsubscribe function.

        """
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    async def start(self) -> None:
        """Start the runtime client."""
        if self._protocol:
            return

        self._protocol = await create_multicast_socket(
            self._interface, self._on_receive
        )
        _LOGGER.debug("HEMS runtime client started on %s", self._interface)

        # Start periodic node probe
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the runtime client."""
        if not self._protocol:
            return

        # Cancel poll task
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

        # Cancel pending Get requests
        for _tid, (_addr, _deoj, _epcs, future) in list(self._pending_gets.items()):
            if not future.done():
                future.cancel()
        self._pending_gets.clear()

        self._protocol.close()
        self._protocol = None
        _LOGGER.debug("HEMS runtime client stopped")

    async def async_probe_nodes(self) -> bool:
        """Send node probe request to discover devices.

        Sends a multicast Get request to node profile for EPCs configured
        during client initialization (default: identification number and
        instance list, plus any extra_epcs).

        Returns:
            True if probe was sent successfully.

        """
        if not self._protocol:
            return False

        frame = Frame(
            tid=Frame.next_tid(),
            seoj=CONTROLLER_INSTANCE.to_bytes(3, "big"),
            deoj=NODE_PROFILE_INSTANCE.to_bytes(3, "big"),
            esv=ESV_GET,
            properties=[Property(epc=epc) for epc in self._discovery_epcs],
        )
        return await self._async_send_to_address(frame, ECHONET_MULTICAST)

    async def async_get(
        self,
        node_id: str,
        deoj: int,
        epcs: list[int],
        seoj: int = CONTROLLER_INSTANCE,
        request_timeout: float = GET_TIMEOUT,
        max_retries: int = GET_MAX_RETRIES,
    ) -> list[Property]:
        """Send Get request to a device by node ID.

        Sends ESV=0x62 Get request and waits for ESV=0x72 (success) or
        ESV=0x52 (partial) response. For 0x52 responses, automatically
        retries failed EPCs.

        Per ECHONET Lite specification, 0x52 response returns properties
        in the same order as requested, with failed properties at the end.
        This allows us to identify which EPCs need retry.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ (3 bytes as int).
            epcs: List of EPCs to read.
            seoj: Source EOJ (default: controller).
            request_timeout: Timeout in seconds for each request.
            max_retries: Maximum retry attempts for failed properties.

        Returns:
            List of Property objects with values.
            Properties that couldn't be read have empty edt.

        """
        address = self._device_addresses.inverse.get(node_id)
        if not address:
            _LOGGER.warning("No address known for device %s", node_id)
            return []

        if not self._protocol or not epcs:
            return []

        received: dict[int, Property] = {}
        remaining_epcs = list(epcs)
        tid = Frame.next_tid()

        try:
            for attempt in range(max_retries + 1):
                if not remaining_epcs:
                    break
                remaining_epcs = await self._attempt_get_request(
                    address,
                    deoj,
                    seoj,
                    remaining_epcs,
                    request_timeout,
                    attempt,
                    tid,
                    received,
                )
        finally:
            if remaining_epcs:
                _LOGGER.debug(
                    "Partial response from %s EOJ 0x%06X, missing EPCs: %s",
                    address,
                    deoj,
                    [f"0x{epc:02X}" for epc in remaining_epcs],
                )

        return [received.get(epc, Property(epc=epc, edt=b"")) for epc in epcs]

    async def async_send(self, node_id: str, frame: Frame) -> bool:
        """Send a frame to a device by node ID.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            frame: Frame to send.

        Returns:
            True if sent successfully.

        """
        address = self._device_addresses.inverse.get(node_id)
        if not address:
            _LOGGER.warning("No address known for device %s", node_id)
            return False
        return await self._async_send_to_address(frame, address)

    def _on_receive(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle received UDP data."""
        try:
            frame = Frame.decode(data)
            address = addr[0]

            # Discard request ESVs (0x60-0x6F) - loopback or other node requests
            if 0x60 <= frame.esv <= 0x6F:
                return

            # Check if this is a response to a pending async_get request
            if (
                frame.esv in (ESV_GET_RES, ESV_GET_SNA)
                and frame.tid in self._pending_gets
            ):
                pending_get = self._pending_gets.pop(frame.tid)
                req_address, _req_deoj, _req_epcs, future = pending_get
                if address == req_address and not future.done():
                    future.set_result(frame.properties)
                # Continue processing to also dispatch the event

            seoj_int = int.from_bytes(frame.seoj, "big")
            seoj_class = seoj_int >> 8

            # Handle node profile responses (identification and instance list)
            if seoj_class == NODE_PROFILE_CLASS:
                self._handle_node_profile(frame, address)
                return

            # For non-node-profile frames, lookup node_id by address
            node_id = self._device_addresses.get(address)
            if not node_id:
                _LOGGER.debug(
                    "Received frame from unknown device at %s (EOJ: 0x%06X), "
                    "queuing and probing",
                    address,
                    seoj_int,
                )
                # Queue the frame for later processing
                pending_frames = self._pending_frames.setdefault(address, [])
                pending_frames.append((frame, seoj_int, time.monotonic()))
                # Trigger node probe to discover the device
                task = asyncio.create_task(self.async_probe_nodes())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return

            # Dispatch frame event
            self._dispatch(
                HemsFrameEvent(
                    received_at=time.monotonic(),
                    frame=frame,
                    node_id=node_id,
                    eoj=seoj_int,
                )
            )
        except Exception as ex:
            _LOGGER.debug("Failed to decode frame from %s: %s", addr, ex)
            self._dispatch(HemsErrorEvent(received_at=time.monotonic(), error=ex))

    def _handle_node_profile(self, frame: Frame, address: str) -> None:
        """Handle node profile responses."""
        # Extract node_id and instances using shared logic
        node_id, instances = extract_discovery_info(frame)

        if not node_id:
            return

        # Collect all properties with non-empty EDT
        properties = {p.epc: p.edt for p in frame.properties if p.edt}

        _LOGGER.debug(
            "Node profile for %s: EPCs=%s",
            node_id,
            [f"0x{epc:02X}" for epc in properties],
        )

        # Use forceput to handle address changes
        self._device_addresses.forceput(address, node_id)

        # Process pending frames for this device if we have node_id
        pending_frames = self._pending_frames.pop(address, None)
        if pending_frames:
            for pending_frame, pending_eoj, pending_received_at in pending_frames:
                self._dispatch(
                    HemsFrameEvent(
                        received_at=pending_received_at,
                        frame=pending_frame,
                        node_id=node_id,
                        eoj=pending_eoj,
                    )
                )

        # Dispatch instance list event if we have node_id
        if instances:
            self._dispatch(
                HemsInstanceListEvent(
                    received_at=time.monotonic(),
                    instances=instances,
                    node_id=node_id,
                    properties=properties,
                )
            )

    def _dispatch(self, event: RuntimeEvent) -> None:
        """Dispatch an event to all subscribers."""
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception:
                _LOGGER.exception("Error in runtime event callback")

    async def _poll_loop(self) -> None:
        """Periodic polling loop for node probe."""
        while self._protocol:
            try:
                await self.async_probe_nodes()
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.exception("Error in poll loop")

    async def _attempt_get_request(
        self,
        address: str,
        deoj: int,
        seoj: int,
        epcs: list[int],
        request_timeout: float,
        attempt: int,
        tid: int,
        received: dict[int, Property],
    ) -> list[int]:
        """Attempt a single Get request and return remaining EPCs."""
        future: asyncio.Future[list[Property]] = asyncio.Future()
        self._pending_gets[tid] = (address, deoj, epcs, future)

        frame = Frame(
            tid=tid,
            seoj=seoj.to_bytes(3, "big"),
            deoj=deoj.to_bytes(3, "big"),
            esv=ESV_GET,
            properties=[Property(epc=epc) for epc in epcs],
        )

        if attempt > 0:
            _LOGGER.debug(
                "Retrying Get request (attempt %d) to %s EOJ 0x%06X for EPCs: [%s]",
                attempt + 1,
                address,
                deoj,
                " ".join(f"{epc:02X}" for epc in epcs),
            )

        if not await self._async_send_to_address(frame, address):
            self._pending_gets.pop(tid, None)
            return epcs

        try:
            response_props = await asyncio.wait_for(
                asyncio.shield(future), request_timeout
            )
        except TimeoutError:
            if not future.done():
                _LOGGER.debug(
                    "Get request to %s EOJ 0x%06X timed out (attempt %d)",
                    address,
                    deoj,
                    attempt + 1,
                )
                return epcs
            response_props = future.result()
        finally:
            self._pending_gets.pop(tid, None)

        # Process response - properties are in request order
        # Failed properties (in 0x52 response) are at the end
        for prop in response_props:
            if prop.edt:  # Successfully read
                received[prop.epc] = prop

        # Check which EPCs were successfully received OR returned as SNA (empty)
        # Per spec: 0x52 returns properties in order, failed ones at end
        # We treat SNA properties (present in response but empty) as "received" (empty)
        # to prevent infinite retries for unsupported/unavailable properties
        received_or_sna = {p.epc for p in response_props}
        return [epc for epc in epcs if epc not in received_or_sna]

    async def _async_send_to_address(self, frame: Frame, address: str) -> bool:
        """Send a frame to a specific address.

        Args:
            frame: Frame to send.
            address: Target IP address.

        Returns:
            True if sent successfully.

        """
        if not self._protocol:
            return False

        # Assign TID if not set (0)
        if frame.tid == 0:
            frame.tid = Frame.next_tid()

        try:
            self._protocol.send(frame.encode(), address)
        except OSError:
            _LOGGER.exception("Failed to send frame to %s", address)
            return False
        else:
            return True
