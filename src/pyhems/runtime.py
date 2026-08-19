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
    ESV_INF,
    ESV_INF_REQ,
    ESV_INF_SNA,
    ESV_INFC,
    ESV_INFC_RES,
    ESV_SETC,
    GET_MAX_RETRIES,
    NODE_PROFILE_CLASS,
    NODE_PROFILE_INSTANCE,
    SETUP_REQUEST_TIMEOUT,
)
from .discovery import _extract_discovery_info
from .eoj import EOJ
from .frame import Frame, Property
from .transport import EchonetLiteProtocol, create_multicast_socket

_LOGGER = logging.getLogger(__name__)


def _format_frame(frame: Frame) -> str:
    """Format frame metadata for debug logging without dumping EDT payloads."""
    epcs = " ".join(f"{prop.epc:02X}" for prop in frame.properties)
    return (
        f"TID=0x{frame.tid:04X} SEOJ={frame.seoj!r} DEOJ={frame.deoj!r} "
        f"ESV=0x{frame.esv:02X} EPCs=[{epcs}]"
    )


@dataclass(slots=True)
class RuntimeEvent:
    """Base class for runtime events."""

    received_at: float


@dataclass(slots=True)
class HemsFrameEvent(RuntimeEvent):
    """Event containing a received ECHONET Lite frame."""

    frame: Frame
    node_id: str
    eoj: EOJ


@dataclass(slots=True)
class HemsInstanceListEvent(RuntimeEvent):
    """Event containing discovered instances from a device.

    Attributes:
        instances: List of EOJs discovered on the device.
        node_id: Device node ID (hex string from EPC 0x83).
        properties: All properties from the node profile response.
            Key is EPC, value is EDT bytes.

    """

    instances: list[EOJ]
    node_id: str
    properties: dict[int, bytes]


@dataclass(slots=True)
class HemsErrorEvent(RuntimeEvent):
    """Event indicating a runtime error."""

    error: Exception


@dataclass(frozen=True, slots=True)
class NotificationRequestResult:
    """Result of an ``ESV_INF_REQ`` (0x63) notification-subscription request.

    Per ECHONET Lite, a device that supports the request responds with
    ``ESV_INF`` (0x73) listing EPCs it will now notify, or ``ESV_INF_SNA``
    (0x53) listing EPCs it could not subscribe to. Requested EPCs missing
    from either response, or for which no response arrives before the
    request times out, are reported as ``unanswered_epcs`` so callers can
    fall back to polling them instead of assuming notifications will
    arrive.
    """

    successful_epcs: frozenset[int]
    failed_epcs: frozenset[int]
    unanswered_epcs: frozenset[int]


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
        self._pending_frames: dict[str, list[tuple[Frame, EOJ, float]]] = {}
        # Background tasks that need to be kept alive
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._poll_task: asyncio.Task[None] | None = None
        # Pending Get requests: tid -> (address, deoj, requested_epcs, future)
        self._pending_gets: dict[
            int, tuple[str, EOJ, list[int], asyncio.Future[list[Property]]]
        ] = {}
        # Pending INF_REQ (0x63) requests awaiting a 0x73/0x53 response:
        # tid -> (address, requested_epcs, future)
        self._pending_infs: dict[
            int, tuple[str, frozenset[int], asyncio.Future[Frame]]
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

        # Cancel pending INF_REQ (0x63) requests
        for _tid, (_addr, _inf_epcs, inf_future) in list(self._pending_infs.items()):
            if not inf_future.done():
                inf_future.cancel()
        self._pending_infs.clear()

        self._protocol.close()
        self._protocol = None
        _LOGGER.debug("HEMS runtime client stopped")

    async def probe_nodes(self) -> bool:
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
            seoj=CONTROLLER_INSTANCE,
            deoj=NODE_PROFILE_INSTANCE,
            esv=ESV_GET,
            properties=[Property(epc=epc) for epc in self._discovery_epcs],
        )
        return await self._send_to_address(frame, ECHONET_MULTICAST)

    async def get(
        self,
        node_id: str,
        deoj: EOJ,
        epcs: list[int],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SETUP_REQUEST_TIMEOUT,
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
            deoj: Destination EOJ.
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
                    "Partial response from %s %r, missing EPCs: %s",
                    address,
                    deoj,
                    [f"0x{epc:02X}" for epc in remaining_epcs],
                )

        return [received.get(epc, Property(epc=epc, edt=b"")) for epc in epcs]

    async def send(self, node_id: str, frame: Frame) -> bool:
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
        return await self._send_to_address(frame, address)

    async def request_notifications(
        self,
        node_id: str,
        deoj: EOJ,
        epcs: list[int],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SETUP_REQUEST_TIMEOUT,
    ) -> NotificationRequestResult:
        """Send an INF_REQ (0x63) and wait for its 0x73/0x53 response.

        Unlike :meth:`get`, this makes a single attempt (no retries): an
        EPC that is not confirmed successful — whether explicitly rejected
        (0x53), absent from the response, or unanswered before
        ``request_timeout`` — is reported in ``unanswered_epcs``/
        ``failed_epcs`` so the caller can fall back to polling it rather
        than assuming a notification will eventually arrive.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ.
            epcs: EPCs to request notifications for.
            seoj: Source EOJ (default: controller).
            request_timeout: Timeout in seconds for the response.

        Returns:
            The outcome, split into successful/failed/unanswered EPCs.

        """
        requested = frozenset(epcs)
        address = self._device_addresses.inverse.get(node_id)
        if not address or not self._protocol or not epcs:
            return NotificationRequestResult(
                successful_epcs=frozenset(),
                failed_epcs=frozenset(),
                unanswered_epcs=requested,
            )

        tid = Frame.next_tid()
        future: asyncio.Future[Frame] = asyncio.Future()
        self._pending_infs[tid] = (address, requested, future)

        frame = Frame(
            tid=tid,
            seoj=seoj,
            deoj=deoj,
            esv=ESV_INF_REQ,
            properties=[Property(epc=epc) for epc in epcs],
        )

        if not await self._send_to_address(frame, address):
            self._pending_infs.pop(tid, None)
            return NotificationRequestResult(
                successful_epcs=frozenset(),
                failed_epcs=frozenset(),
                unanswered_epcs=requested,
            )

        try:
            response_frame = await asyncio.wait_for(
                asyncio.shield(future), request_timeout
            )
        except TimeoutError:
            if not future.done():
                _LOGGER.debug(
                    "Notification request (0x63) to %s %r timed out for EPCs: [%s]",
                    address,
                    deoj,
                    " ".join(f"{epc:02X}" for epc in sorted(requested)),
                )
                return NotificationRequestResult(
                    successful_epcs=frozenset(),
                    failed_epcs=frozenset(),
                    unanswered_epcs=requested,
                )
            response_frame = future.result()
        finally:
            self._pending_infs.pop(tid, None)

        responded_epcs = frozenset(prop.epc for prop in response_frame.properties)
        # Per spec, 0x73 (INF) means the whole request was accepted and
        # 0x53 (INF_SNA) means at least one EPC was rejected. Devices are
        # not required to echo the full requested EPC set in either case,
        # so any requested EPC absent from the response is treated as
        # unanswered (the safe default: fall back to polling it) rather
        # than assumed successful.
        if response_frame.esv == ESV_INF:
            successful = responded_epcs & requested
            failed: frozenset[int] = frozenset()
        else:
            successful = frozenset()
            failed = responded_epcs & requested
        unanswered = requested - responded_epcs

        return NotificationRequestResult(
            successful_epcs=successful,
            failed_epcs=failed,
            unanswered_epcs=unanswered,
        )

    async def set_property(
        self,
        node_id: str,
        deoj: EOJ,
        epc: int,
        edt: bytes,
        seoj: EOJ = CONTROLLER_INSTANCE,
    ) -> bool:
        """Send a SetC request with a single EPC/EDT pair.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ.
            epc: Property code.
            edt: Property value.
            seoj: Source EOJ (default: controller).

        Returns:
            True if sent successfully.
        """
        return await self.set_properties(
            node_id=node_id,
            deoj=deoj,
            properties=[Property(epc=epc, edt=edt)],
            seoj=seoj,
        )

    async def set_properties(
        self,
        node_id: str,
        deoj: EOJ,
        properties: list[Property],
        seoj: EOJ = CONTROLLER_INSTANCE,
    ) -> bool:
        """Send a SetC request with multiple properties.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ.
            properties: List of properties to write.
            seoj: Source EOJ (default: controller).

        Returns:
            True if sent successfully.
        """
        if not properties:
            return False

        frame = Frame(
            seoj=seoj,
            deoj=deoj,
            esv=ESV_SETC,
            properties=properties,
        )
        return await self.send(node_id, frame)

    def _on_receive(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle received UDP data."""
        try:
            frame = Frame.decode(data)
            address = addr[0]
            _LOGGER.debug(
                "Received ECHONET Lite frame from %s:%d: %s",
                addr[0],
                addr[1],
                _format_frame(frame),
            )

            # Discard request ESVs (0x60-0x6F) - loopback or other node requests
            if 0x60 <= frame.esv <= 0x6F:
                _LOGGER.debug("Discarding request frame: %s", _format_frame(frame))
                return

            # Check if this is a response to a pending get request
            if (
                frame.esv in (ESV_GET_RES, ESV_GET_SNA)
                and frame.tid in self._pending_gets
            ):
                pending_get = self._pending_gets.pop(frame.tid)
                req_address, _req_deoj, _req_epcs, future = pending_get
                if address == req_address and not future.done():
                    _LOGGER.debug(
                        "Matched pending Get response from %s: %s",
                        address,
                        _format_frame(frame),
                    )
                    future.set_result(frame.properties)
                elif address != req_address:
                    _LOGGER.debug(
                        "Ignoring pending Get response from unexpected address %s "
                        "(expected %s): %s",
                        address,
                        req_address,
                        _format_frame(frame),
                    )
                # Continue processing to also dispatch the event

            # Check if this is a response to a pending INF_REQ (0x63) request
            if frame.esv in (ESV_INF, ESV_INF_SNA) and frame.tid in self._pending_infs:
                pending_inf = self._pending_infs.pop(frame.tid)
                req_address, _req_inf_epcs, inf_future = pending_inf
                if address == req_address and not inf_future.done():
                    _LOGGER.debug(
                        "Matched pending notification-request response from %s: %s",
                        address,
                        _format_frame(frame),
                    )
                    inf_future.set_result(frame)
                elif address != req_address:
                    _LOGGER.debug(
                        "Ignoring pending notification-request response from "
                        "unexpected address %s (expected %s): %s",
                        address,
                        req_address,
                        _format_frame(frame),
                    )
                # Continue processing to also dispatch the event (0x73 also
                # carries a real notification other subscribers need to see)

            # Handle node profile responses (identification and instance list)
            if frame.seoj.class_code == NODE_PROFILE_CLASS:
                self._handle_node_profile(frame, address)
                return

            # Send INFC_RES confirmation for INFC (0x74) frames
            if frame.esv == ESV_INFC:
                _LOGGER.debug(
                    "Received INFC (0x74) from %s (EOJ: %r), sending INFC_RES",
                    address,
                    frame.seoj,
                )
                infc_res = Frame(
                    tid=frame.tid,
                    seoj=frame.deoj,
                    deoj=frame.seoj,
                    esv=ESV_INFC_RES,
                    properties=frame.properties,
                )
                task = asyncio.create_task(self._send_to_address(infc_res, address))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            # For non-node-profile frames, lookup node_id by address
            node_id = self._device_addresses.get(address)
            if not node_id:
                _LOGGER.debug(
                    "Received frame from unknown device at %s (EOJ: %r), "
                    "queuing and probing",
                    address,
                    frame.seoj,
                )
                # Queue the frame for later processing
                pending_frames = self._pending_frames.setdefault(address, [])
                pending_frames.append((frame, frame.seoj, time.monotonic()))
                # Trigger node probe to discover the device
                task = asyncio.create_task(self.probe_nodes())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return

            # Dispatch frame event
            self._dispatch(
                HemsFrameEvent(
                    received_at=time.monotonic(),
                    frame=frame,
                    node_id=node_id,
                    eoj=frame.seoj,
                )
            )
        except Exception as ex:
            _LOGGER.debug("Failed to decode frame from %s: %s", addr, ex)
            self._dispatch(HemsErrorEvent(received_at=time.monotonic(), error=ex))

    def _handle_node_profile(self, frame: Frame, address: str) -> None:
        """Handle node profile responses."""
        # Extract node_id and instances using shared logic
        node_id, instances = _extract_discovery_info(frame)

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
                await asyncio.sleep(self._poll_interval)
                await self.probe_nodes()
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.exception("Error in poll loop")

    async def _attempt_get_request(
        self,
        address: str,
        deoj: EOJ,
        seoj: EOJ,
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
            seoj=seoj,
            deoj=deoj,
            esv=ESV_GET,
            properties=[Property(epc=epc) for epc in epcs],
        )

        if attempt > 0:
            _LOGGER.debug(
                "Retrying Get request (attempt %d) to %s %r for EPCs: [%s]",
                attempt + 1,
                address,
                deoj,
                " ".join(f"{epc:02X}" for epc in epcs),
            )

        if not await self._send_to_address(frame, address):
            self._pending_gets.pop(tid, None)
            return epcs

        try:
            response_props = await asyncio.wait_for(
                asyncio.shield(future), request_timeout
            )
        except TimeoutError:
            if not future.done():
                _LOGGER.debug(
                    "Get request to %s %r timed out (attempt %d)",
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

    async def _send_to_address(self, frame: Frame, address: str) -> bool:
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
            _LOGGER.debug(
                "Sending ECHONET Lite frame to %s: %s",
                address,
                _format_frame(frame),
            )
            self._protocol.send(frame.encode(), address)
        except OSError:
            _LOGGER.exception("Failed to send frame to %s", address)
            return False
        else:
            return True
