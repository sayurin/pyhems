"""HEMS runtime client for ECHONET Lite communication."""

import asyncio
import contextlib
import heapq
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from bidict import bidict

from .const import (
    CONTROLLER_INSTANCE,
    DISCOVERY_DEFAULT_EPCS,
    DISCOVERY_INITIAL_EPCS,
    ECHONET_MULTICAST,
    EPC,
    ESV,
    NODE_PROFILE_CLASS,
    NODE_PROFILE_INSTANCE,
    SET_REQUEST_TIMEOUT,
    SETUP_REQUEST_TIMEOUT,
)
from .discovery import _extract_discovery_info
from .eoj import EOJ
from .frame import Frame, FrameDecodeError, Property
from .get_batch_policy import (
    has_get_batch_policy_for_class,
    take_first_batch,
)
from .transport import EchonetLiteProtocol, create_multicast_socket

_LOGGER = logging.getLogger(__name__)
_INITIAL_DISCOVERY_DELAY = 30.0
_MAX_PENDING_FRAMES_PER_ADDRESS = 16
_MAX_PENDING_ADDRESSES = 32


def _format_frame(frame: Frame) -> str:
    """Format frame metadata for debug logging without dumping EDT payloads."""
    epcs = " ".join(f"{prop.epc:02X}" for prop in frame.properties)
    return (
        f"TID=0x{frame.tid:04X} SEOJ={frame.seoj!r} DEOJ={frame.deoj!r} "
        f"ESV=0x{frame.esv:02X} EPCs=[{epcs}]"
    )


def _format_epcs(epcs: list[int]) -> str:
    """Format EPCs for log messages."""
    return " ".join(f"{epc:02X}" for epc in epcs)


class GetAbortedError(Exception):
    """Raised when reading a device object is given up.

    Raised by :meth:`HemsClient.setup_get` when a single-EPC request (including
    the standalone manufacturer code read) is not answered, or a request could
    not be sent. Only the affected device object is given up.
    """


class ClientStoppedError(Exception):
    """Raised when a pending request is interrupted by :meth:`HemsClient.stop`."""


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

    """

    instances: list[EOJ]
    node_id: str


@dataclass(slots=True)
class HemsErrorEvent(RuntimeEvent):
    """Event indicating a runtime error."""

    error: Exception


@dataclass(frozen=True, slots=True)
class InfRequestResult:
    """Result of an ``ESV.INF_REQ`` (0x63) request.

    INF_REQ asks a device to notify the current values of the requested EPCs
    once. A device that supports the request responds with ``ESV.INF``
    (0x73) carrying the values it notified (``notified_epcs``), or
    ``ESV.INF_SNA`` (0x53) listing the EPCs it rejected (``rejected_epcs``).
    Requested EPCs missing from either response, or for which no response
    arrives before the request times out, are reported as
    ``unanswered_epcs``.
    """

    notified_epcs: frozenset[int]
    rejected_epcs: frozenset[int]
    unanswered_epcs: frozenset[int]


@dataclass(frozen=True, slots=True)
class SetRequestResult:
    """Result of an acknowledged ESV.SETC request."""

    sent: bool
    response_esv: ESV | None
    accepted_epcs: frozenset[int]
    rejected_epcs: frozenset[int]
    unanswered_epcs: frozenset[int]


@dataclass(frozen=True, slots=True)
class GetResponse:
    """Response received for one ESV.GET request.

    Attributes:
        esv: ``ESV.GET_RES`` or ``ESV.GET_SNA``.
        properties: Properties in the response frame, in frame order.
        latency: Seconds from sending the request to receiving the response.
    """

    esv: ESV
    properties: tuple[Property, ...]
    latency: float


class RequestPriority(IntEnum):
    """Order in which queued requests to one node are sent (ADR-0011).

    Lower values are sent first; requests with the same priority are sent in
    arrival order.
    """

    SET = 0
    SETUP = 1
    POLL = 2


_GET_RESPONSE_ESVS = frozenset({ESV.GET_RES, ESV.GET_SNA})
_SET_RESPONSE_ESVS = frozenset({ESV.SET_RES, ESV.SETC_SNA})
_INF_REQ_RESPONSE_ESVS = frozenset({ESV.INF, ESV.INF_SNA})


@dataclass(slots=True)
class _PendingRequest:
    """A request sent to a device and waiting for its response."""

    address: str
    deoj: EOJ
    seoj: EOJ
    response_esvs: frozenset[ESV]
    future: asyncio.Future[Frame]


class _NodeRequestQueue:
    """Allow one outstanding request per node, granted by priority (ADR-0011)."""

    def __init__(self) -> None:
        self._busy = False
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._sequence = itertools.count()

    @property
    def idle(self) -> bool:
        """Return whether no request holds or waits for the slot."""
        return not self._busy and not self._waiters

    async def acquire(self, priority: RequestPriority) -> None:
        """Wait until this request may be sent to the node."""
        if self.idle:
            self._busy = True
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._sequence), future))
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                # The slot was handed over just before the cancellation.
                self.release()
            raise

    def release(self) -> None:
        """Hand the slot to the next waiting request, if any."""
        while self._waiters:
            _priority, _sequence, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)
                return
        self._busy = False

    def fail_waiters(self, error: Exception) -> None:
        """Fail every waiting request with ``error``."""
        for _priority, _sequence, future in self._waiters:
            if not future.done():
                future.set_exception(error)
        self._waiters.clear()


@dataclass(slots=True)
class _GetCapability:
    """Observed GET response behavior for one device object."""

    # Concurrent-response limit; None until a valid response fixes it.
    observed_batch_capacity: int | None = None
    manufacturer_code: int | None = None


EventCallback = Callable[[RuntimeEvent], None]


class HemsClient:
    """Runtime client for ECHONET Lite HEMS communication."""

    def __init__(
        self,
        interface: str = "0.0.0.0",
        discovery_interval: float = 60.0 * 60.0,
    ) -> None:
        """Initialize the HEMS client.

        Args:
            interface: Network interface IP to bind to.
            discovery_interval: Interval for recurring node discovery
                (seconds).

        """
        self._interface = interface
        self._discovery_interval = discovery_interval
        self._initial_discovery_epcs = list(DISCOVERY_INITIAL_EPCS)
        self._protocol: EchonetLiteProtocol | None = None
        self._callbacks: list[EventCallback] = []
        # address <-> node_id (hex) bidirectional mapping
        self._device_addresses: bidict[str, str] = bidict()
        # Queue to store frames from unknown devices (frame, eoj, received_at)
        self._pending_frames: dict[str, list[tuple[Frame, EOJ, float]]] = {}
        # Per-address direct node discovery tasks started by unknown discovery
        # frames.
        self._address_discovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._discovery_task: asyncio.Task[None] | None = None
        # Requests waiting for their response, keyed by TID.
        self._pending: dict[int, _PendingRequest] = {}
        # Per-address queues that serialize requests to one node (ADR-0011).
        self._node_queues: dict[str, _NodeRequestQueue] = {}
        self._tid_counter = 0
        # Observed GET response behavior, scoped to a node and destination EOJ.
        # This is intentionally runtime-only and is relearned after restart.
        self._get_capabilities: dict[tuple[str, EOJ], _GetCapability] = {}
        self._decode_error_count = 0

    @property
    def decode_error_count(self) -> int:
        """Return the number of received datagrams that could not be decoded."""
        return self._decode_error_count

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
            self._interface, self._on_receive, error_callback=self._on_transport_error
        )
        _LOGGER.debug("HEMS runtime client started on %s", self._interface)
        if not self.probe_initial_nodes():
            await self.stop()
            raise OSError("Initial ECHONET Lite node discovery could not be sent")
        self.start_periodic_discovery()

    async def stop(self) -> None:
        """Stop the runtime client."""
        discovery_tasks = list(self._address_discovery_tasks.values())
        for task in discovery_tasks:
            task.cancel()
        if discovery_tasks:
            await asyncio.gather(*discovery_tasks, return_exceptions=True)
        self._address_discovery_tasks.clear()
        self._pending_frames.clear()
        self._get_capabilities.clear()

        if not self._protocol:
            return

        if self._discovery_task:
            self._discovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._discovery_task
            self._discovery_task = None

        # Fail pending and queued requests instead of cancelling them:
        # cancellation would propagate into, and terminate, the callers' tasks.
        pending = list(self._pending.values())
        self._pending.clear()
        for request in pending:
            if not request.future.done():
                request.future.set_exception(
                    ClientStoppedError("Runtime client stopped")
                )
        queues = list(self._node_queues.values())
        self._node_queues.clear()
        for queue in queues:
            queue.fail_waiters(ClientStoppedError("Runtime client stopped"))

        self._protocol.close()
        self._protocol = None
        _LOGGER.debug("HEMS runtime client stopped")

    def _next_transaction_id(self) -> int:
        """Return a transaction ID that is not used by a pending request."""
        for _ in range(0xFFFF):
            self._tid_counter = self._tid_counter % 0xFFFF + 1
            if self._tid_counter not in self._pending:
                return self._tid_counter
        raise RuntimeError("No transaction ID is available")

    def _clear_get_capabilities_for_node(self, node_id: str) -> None:
        """Discard learned GET behavior for a node no longer in use."""
        for key in tuple(self._get_capabilities):
            if key[0] == node_id:
                del self._get_capabilities[key]

    def probe_nodes(self) -> bool:
        """Send a recurring node probe request to discover devices.

        Sends a multicast Get request to the node profile for the self-node
        instance list (EPC 0xD6). This request is intentionally limited to one
        property so it can be used for recurring discovery.

        Returns:
            True if probe was sent successfully.

        """
        if not self._protocol:
            return False

        return self._probe_nodes(DISCOVERY_DEFAULT_EPCS)

    def probe_initial_nodes(self) -> bool:
        """Send the initial multicast node probe request."""
        return self._probe_nodes(self._initial_discovery_epcs)

    def _probe_nodes(self, epcs: list[int]) -> bool:
        """Send a multicast node probe request for the given EPCs."""
        if not self._protocol:
            return False

        frame = Frame(
            tid=self._next_transaction_id(),
            seoj=CONTROLLER_INSTANCE,
            deoj=NODE_PROFILE_INSTANCE,
            esv=ESV.GET,
            properties=[Property(epc=epc) for epc in epcs],
        )
        return self._send_to_address(frame, ECHONET_MULTICAST)

    def start_periodic_discovery(self) -> None:
        """Start recurring node discovery immediately."""
        if self._protocol and self._discovery_task is None:
            self._discovery_task = asyncio.create_task(self._discovery_loop())

    async def get(
        self,
        node_id: str,
        deoj: EOJ,
        epcs: list[int],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SETUP_REQUEST_TIMEOUT,
        priority: RequestPriority = RequestPriority.SETUP,
    ) -> list[Property]:
        """Send Get requests to a device by node ID.

        The EPCs are split by the device's known concurrent-response limit
        and by device-specific GET policies, and each batch is sent once with
        a new TID through the node's request queue. EPCs omitted from a
        partial response are requested again in a later batch. Unanswered
        requests are not retried, and the concurrent-response limit is
        neither probed nor learned here; see :meth:`setup_get`.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ.
            epcs: List of EPCs to read.
            seoj: Source EOJ (default: controller).
            request_timeout: Timeout in seconds for each request.
            priority: Queue priority of the requests.

        Returns:
            One Property per requested EPC, in the requested order.
            Properties that couldn't be read have empty edt.

        """
        unread = [Property(epc=epc, edt=b"") for epc in epcs]
        address = self._device_addresses.inverse.get(node_id)
        if not address:
            _LOGGER.warning("No address known for device %s", node_id)
            return unread
        if not self._protocol or not epcs:
            return unread

        received: dict[int, Property] = {}
        capability = self._get_capabilities.setdefault(
            (node_id, deoj), _GetCapability()
        )
        try:
            await self._get_at_address(
                address=address,
                deoj=deoj,
                epcs=epcs,
                seoj=seoj,
                request_timeout=request_timeout,
                capability=capability,
                received=received,
                priority=priority,
            )
        except (GetAbortedError, ClientStoppedError) as err:
            _LOGGER.debug("Get request to %s %r stopped: %s", address, deoj, err)
        return [received.get(epc, Property(epc=epc, edt=b"")) for epc in epcs]

    async def request_get(
        self,
        node_id: str,
        deoj: EOJ,
        epcs: list[int],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SETUP_REQUEST_TIMEOUT,
        priority: RequestPriority = RequestPriority.POLL,
    ) -> GetResponse | None:
        """Send one Get request without splitting it, and wait for its response.

        The request is sent once through the node's request queue. Callers
        are responsible for keeping ``epcs`` within the device's limits (see
        :meth:`get_observed_batch_capacity` and
        :func:`~pyhems.take_first_batch`).

        Returns:
            The response, or None if the request could not be sent, was not
            answered before ``request_timeout``, or the client was stopped.
        """
        address = self._device_addresses.inverse.get(node_id)
        if not address or not epcs:
            return None
        try:
            _sent, response = await self._attempt_get_request(
                address, deoj, seoj, epcs, request_timeout, priority
            )
        except ClientStoppedError:
            return None
        return response

    async def read_manufacturer_code(
        self,
        node_id: str,
        deoj: EOJ,
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SETUP_REQUEST_TIMEOUT,
    ) -> int:
        """Return the manufacturer code (EPC 0x8A) of a device object.

        The code is read on its own the first time and then remembered for
        the device object until the client stops, so device-specific GET
        policies can be applied to later requests.

        Raises:
            GetAbortedError: The code could not be read.
            ClientStoppedError: The client was stopped while reading.
        """
        capability = self._get_capabilities.setdefault(
            (node_id, deoj), _GetCapability()
        )
        if capability.manufacturer_code is not None:
            return capability.manufacturer_code
        address = self._device_addresses.inverse.get(node_id)
        if not address:
            raise GetAbortedError(f"No address known for device {node_id}")
        if not self._protocol:
            raise GetAbortedError("Runtime client is not started")
        if (
            await self._read_manufacturer_code(
                address=address,
                deoj=deoj,
                seoj=seoj,
                request_timeout=request_timeout,
                capability=capability,
                priority=RequestPriority.SETUP,
            )
            is None
            or capability.manufacturer_code is None
        ):
            raise GetAbortedError(
                f"Could not read manufacturer code (EPC 0x8A) from {node_id} {deoj!r}"
            )
        return capability.manufacturer_code

    async def setup_get(
        self,
        node_id: str,
        deoj: EOJ,
        epcs: list[int],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SETUP_REQUEST_TIMEOUT,
    ) -> list[Property]:
        """Read initial device properties while probing the response limit.

        The manufacturer code (EPC 0x8A) is read on its own first if it is not
        known yet, so device-specific GET policies can be applied. The
        remaining EPCs are then requested in batches. A batch that times out
        is split in half; the device's concurrent-response limit is fixed
        from the number of EPCs a valid response actually processed.

        Raises:
            GetAbortedError: The device object could not be read, e.g. because
                a single-EPC request timed out or the request could not be
                sent. Other device objects are not affected.
            ClientStoppedError: The client was stopped while reading.

        Returns:
            List of Property objects in the requested order.
            Properties that couldn't be read have empty edt.

        """
        address = self._device_addresses.inverse.get(node_id)
        if not address:
            raise GetAbortedError(f"No address known for device {node_id}")
        if not self._protocol:
            raise GetAbortedError("Runtime client is not started")

        capability = self._get_capabilities.setdefault(
            (node_id, deoj), _GetCapability()
        )
        received: dict[int, Property] = {}
        if capability.manufacturer_code is None:
            manufacturer = await self._read_manufacturer_code(
                address=address,
                deoj=deoj,
                seoj=seoj,
                request_timeout=request_timeout,
                capability=capability,
                priority=RequestPriority.SETUP,
            )
            if manufacturer is None:
                raise GetAbortedError(
                    f"Could not read manufacturer code (EPC 0x8A) from {node_id} "
                    f"{deoj!r}"
                )
            received[EPC.MANUFACTURER_CODE] = manufacturer

        await self._run_get_batches(
            address=address,
            deoj=deoj,
            seoj=seoj,
            epcs=[epc for epc in epcs if epc not in received],
            request_timeout=request_timeout,
            capability=capability,
            received=received,
            probe=True,
            priority=RequestPriority.SETUP,
        )
        return [received.get(epc, Property(epc=epc, edt=b"")) for epc in epcs]

    def get_observed_batch_capacity(self, node_id: str, deoj: EOJ) -> int | None:
        """Return the known concurrent GET response limit for a device object."""
        capability = self._get_capabilities.get((node_id, deoj))
        return None if capability is None else capability.observed_batch_capacity

    async def _get_at_address(
        self,
        *,
        address: str,
        deoj: EOJ,
        epcs: list[int],
        seoj: EOJ,
        request_timeout: float,
        capability: _GetCapability,
        received: dict[int, Property],
        priority: RequestPriority,
    ) -> None:
        """Read ``epcs`` into ``received`` without probing the response limit.

        Raises:
            GetAbortedError: A request was not answered or could not be sent.
            ClientStoppedError: The client was stopped.
        """
        if capability.manufacturer_code is None and has_get_batch_policy_for_class(
            deoj.class_code
        ):
            manufacturer = await self._read_manufacturer_code(
                address=address,
                deoj=deoj,
                seoj=seoj,
                request_timeout=request_timeout,
                capability=capability,
                priority=priority,
            )
            if manufacturer is None:
                return
            if EPC.MANUFACTURER_CODE in epcs:
                received[EPC.MANUFACTURER_CODE] = manufacturer

        await self._run_get_batches(
            address=address,
            deoj=deoj,
            seoj=seoj,
            epcs=[epc for epc in epcs if epc not in received],
            request_timeout=request_timeout,
            capability=capability,
            received=received,
            probe=False,
            priority=priority,
        )

    async def _read_manufacturer_code(
        self,
        *,
        address: str,
        deoj: EOJ,
        seoj: EOJ,
        request_timeout: float,
        capability: _GetCapability,
        priority: RequestPriority,
    ) -> Property | None:
        """Read 0x8A on its own before applying a device-specific GET policy.

        The request is sent once. Its response is not used to determine the
        concurrent-response limit.
        """
        manufacturer_epc = EPC.MANUFACTURER_CODE
        _sent, response = await self._attempt_get_request(
            address, deoj, seoj, [manufacturer_epc], request_timeout, priority
        )
        manufacturer_property = (
            None
            if response is None
            else next(
                (
                    prop
                    for prop in response.properties
                    if prop.epc == manufacturer_epc and len(prop.edt) >= 3
                ),
                None,
            )
        )
        if manufacturer_property is None:
            _LOGGER.warning("Could not identify manufacturer for %s %r", address, deoj)
            return None

        capability.manufacturer_code = int.from_bytes(
            manufacturer_property.edt[:3], "big"
        )
        _LOGGER.debug(
            "Identified manufacturer 0x%06X for %s %r before GET batching",
            capability.manufacturer_code,
            address,
            deoj,
        )
        return manufacturer_property

    async def _run_get_batches(
        self,
        *,
        address: str,
        deoj: EOJ,
        seoj: EOJ,
        epcs: list[int],
        request_timeout: float,
        capability: _GetCapability,
        received: dict[int, Property],
        probe: bool,
        priority: RequestPriority,
    ) -> None:
        """Request ``epcs`` in batches, storing values in ``received``.

        Every batch is planned by ``take_first_batch`` from the known
        concurrent-response limit and the device-specific GET policy, and is
        sent once with a new TID. EPCs omitted from a partial response are
        queued again.

        With ``probe`` (setup), an unanswered batch, or a batch whose EPCs all
        came back empty while the limit is unknown, is split in half and the
        limit is fixed from the number of EPCs a valid response processed.
        A single-EPC batch that is not answered raises
        :class:`GetAbortedError`.

        Without ``probe``, the limit is neither probed nor learned, and the
        first unanswered batch raises :class:`GetAbortedError`.
        """
        queue = list(dict.fromkeys(epcs))
        # Batch size used while probing; local to this call and never stored.
        probe_size: int | None = None
        while queue:
            limit = capability.observed_batch_capacity
            size = (
                limit
                if probe_size is None
                else probe_size
                if limit is None
                else min(limit, probe_size)
            )
            batch_tuple, rest = take_first_batch(
                queue,
                manufacturer_code=capability.manufacturer_code,
                class_code=deoj.class_code,
                observed_batch_capacity=size,
            )
            batch = list(batch_tuple)
            n = len(batch)
            sent, response = await self._attempt_get_request(
                address, deoj, seoj, batch, request_timeout, priority
            )
            if not sent:
                raise GetAbortedError(
                    f"Get request to {address} {deoj!r} could not be sent"
                )

            matched: dict[int, Property] = {}
            if response is not None:
                requested = set(batch)
                for prop in response.properties:
                    if prop.epc in requested:
                        matched.setdefault(prop.epc, prop)
                if unexpected := [
                    prop.epc
                    for prop in response.properties
                    if prop.epc not in requested
                ]:
                    _LOGGER.debug(
                        "Ignoring unrequested EPCs in Get response from %s %r: [%s]",
                        address,
                        deoj,
                        " ".join(f"{epc:02X}" for epc in unexpected),
                    )

            if not matched:
                if not probe or n == 1:
                    raise GetAbortedError(
                        f"Get request to {address} {deoj!r} for EPCs "
                        f"[{_format_epcs(batch)}] was not answered"
                    )
                probe_size = (n + 1) // 2
                _LOGGER.debug(
                    "Get request to %s %r for %d EPCs was not answered; "
                    "retrying in batches of %d",
                    address,
                    deoj,
                    n,
                    probe_size,
                )
                queue = batch + list(rest)
                continue

            m = len(matched)
            if m < n:
                # The device processed only m EPCs of this request.
                if probe:
                    self._fix_batch_capacity(capability, m, address, deoj)
                    probe_size = None
                received.update(
                    (epc, prop) for epc, prop in matched.items() if prop.edt
                )
                omitted = [epc for epc in batch if epc not in matched]
                _LOGGER.debug(
                    "Partial Get response from %s %r: %d of %d EPCs, "
                    "requesting [%s] again",
                    address,
                    deoj,
                    m,
                    n,
                    _format_epcs(omitted),
                )
                queue = omitted + list(rest)
                continue

            all_empty = not any(prop.edt for prop in matched.values())
            if probe and all_empty and limit is None and n > 1:
                # ADR-0001: some devices answer every EPC with PDC=0 when the
                # request exceeds their concurrent-response limit.
                probe_size = (n + 1) // 2
                _LOGGER.debug(
                    "Every EPC in the Get response from %s %r for %d EPCs was "
                    "empty; retrying in batches of %d",
                    address,
                    deoj,
                    n,
                    probe_size,
                )
                queue = batch + list(rest)
                continue

            if probe and (probe_size is not None or (limit is None and n > 1)):
                self._fix_batch_capacity(capability, n, address, deoj)
                probe_size = None
            received.update((epc, prop) for epc, prop in matched.items() if prop.edt)
            queue = list(rest)

    @staticmethod
    def _fix_batch_capacity(
        capability: _GetCapability, capacity: int, address: str, deoj: EOJ
    ) -> None:
        """Record a concurrent-response limit, keeping the smallest value."""
        previous = capability.observed_batch_capacity
        if previous is not None and previous <= capacity:
            return
        capability.observed_batch_capacity = capacity
        _LOGGER.debug(
            "Concurrent Get response limit for %s %r is %d", address, deoj, capacity
        )

    def _schedule_address_discovery(self, address: str) -> None:
        """Start direct node discovery for an unknown address once."""
        if address in self._address_discovery_tasks:
            return

        task = asyncio.create_task(self._discover_address(address))
        self._address_discovery_tasks[address] = task

        def done_callback(completed_task: asyncio.Task[None]) -> None:
            if self._address_discovery_tasks.get(address) is completed_task:
                del self._address_discovery_tasks[address]

        task.add_done_callback(done_callback)

    async def _discover_address(self, address: str) -> None:
        """Discover an unknown node directly from its source address."""
        try:
            # The node ID is not known yet, so nothing is learned for it.
            await self._get_at_address(
                address=address,
                deoj=NODE_PROFILE_INSTANCE,
                epcs=self._initial_discovery_epcs,
                seoj=CONTROLLER_INSTANCE,
                request_timeout=SETUP_REQUEST_TIMEOUT,
                capability=_GetCapability(),
                received={},
                priority=RequestPriority.SETUP,
            )
        except (GetAbortedError, ClientStoppedError) as err:
            _LOGGER.debug("Direct discovery of %s stopped: %s", address, err)
        finally:
            if address not in self._device_addresses:
                self._pending_frames.pop(address, None)

    def send(self, node_id: str, frame: Frame) -> bool:
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
        if frame.tid == 0:
            frame.tid = self._next_transaction_id()
        return self._send_to_address(frame, address)

    async def request_notifications(
        self,
        node_id: str,
        deoj: EOJ,
        epcs: list[int],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SETUP_REQUEST_TIMEOUT,
        priority: RequestPriority = RequestPriority.SETUP,
    ) -> InfRequestResult:
        """Send an INF_REQ (0x63) and wait for its 0x73/0x53 response.

        Unlike :meth:`get`, this makes a single attempt (no retries): an
        EPC that is not confirmed successful — whether explicitly rejected
        (0x53), absent from the response, or unanswered before
        ``request_timeout`` — is reported in ``unanswered_epcs``/
        ``rejected_epcs`` so the caller can fall back to polling it rather
        than assuming a notification will eventually arrive.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ.
            epcs: EPCs to request notifications for.
            seoj: Source EOJ (default: controller).
            request_timeout: Timeout in seconds for the response.
            priority: Queue priority of the request.

        Returns:
            The outcome, split into successful/failed/unanswered EPCs.

        """
        requested = frozenset(epcs)
        address = self._device_addresses.inverse.get(node_id)
        if not address or not self._protocol or not epcs:
            return InfRequestResult(
                notified_epcs=frozenset(),
                rejected_epcs=frozenset(),
                unanswered_epcs=requested,
            )

        no_response = InfRequestResult(
            notified_epcs=frozenset(),
            rejected_epcs=frozenset(),
            unanswered_epcs=requested,
        )
        try:
            _sent, response_frame, _latency = await self._send_request(
                address,
                deoj=deoj,
                seoj=seoj,
                esv=ESV.INF_REQ,
                properties=[Property(epc=epc) for epc in epcs],
                response_esvs=_INF_REQ_RESPONSE_ESVS,
                request_timeout=request_timeout,
                priority=priority,
            )
        except ClientStoppedError:
            return no_response
        if response_frame is None:
            return no_response

        responded_epcs = frozenset(prop.epc for prop in response_frame.properties)
        # Per spec, 0x73 (INF) means the whole request was accepted and
        # 0x53 (INF_SNA) means at least one EPC was rejected. Devices are
        # not required to echo the full requested EPC set in either case,
        # so any requested EPC absent from the response is treated as
        # unanswered (the safe default: fall back to polling it) rather
        # than assumed successful.
        if response_frame.esv == ESV.INF:
            successful = responded_epcs & requested
            failed: frozenset[int] = frozenset()
        else:
            successful = frozenset()
            failed = responded_epcs & requested
        unanswered = requested - responded_epcs

        return InfRequestResult(
            notified_epcs=successful,
            rejected_epcs=failed,
            unanswered_epcs=unanswered,
        )

    async def set_property(
        self,
        node_id: str,
        deoj: EOJ,
        epc: int,
        edt: bytes,
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SET_REQUEST_TIMEOUT,
        priority: RequestPriority = RequestPriority.SET,
    ) -> SetRequestResult:
        """Send a SetC request with a single EPC/EDT pair.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ.
            epc: Property code.
            edt: Property value.
            seoj: Source EOJ (default: controller).
            request_timeout: Timeout in seconds for the response.
            priority: Queue priority of the request.

        Returns:
            The acknowledged SetC result.
        """
        return await self.set_properties(
            node_id=node_id,
            deoj=deoj,
            properties=[Property(epc=epc, edt=edt)],
            seoj=seoj,
            request_timeout=request_timeout,
            priority=priority,
        )

    async def set_properties(
        self,
        node_id: str,
        deoj: EOJ,
        properties: list[Property],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SET_REQUEST_TIMEOUT,
        priority: RequestPriority = RequestPriority.SET,
    ) -> SetRequestResult:
        """Send a SetC request with multiple properties.

        Args:
            node_id: Device node ID (hex string from EPC 0x83).
            deoj: Destination EOJ.
            properties: List of properties to write.
            seoj: Source EOJ (default: controller).
            request_timeout: Timeout in seconds for the response, not
                including the time spent waiting in the node's queue.
            priority: Queue priority of the request.

        Returns:
            The acknowledged SetC result.
        """
        if not properties:
            return SetRequestResult(
                sent=False,
                response_esv=None,
                accepted_epcs=frozenset(),
                rejected_epcs=frozenset(),
                unanswered_epcs=frozenset(),
            )

        requested_epcs = frozenset(prop.epc for prop in properties)
        address = self._device_addresses.inverse.get(node_id)
        if not address or not self._protocol:
            _LOGGER.warning("No address known for device %s", node_id)
            return SetRequestResult(
                sent=False,
                response_esv=None,
                accepted_epcs=frozenset(),
                rejected_epcs=frozenset(),
                unanswered_epcs=requested_epcs,
            )

        no_response = SetRequestResult(
            sent=True,
            response_esv=None,
            accepted_epcs=frozenset(),
            rejected_epcs=frozenset(),
            unanswered_epcs=requested_epcs,
        )
        try:
            sent, response_frame, _latency = await self._send_request(
                address,
                deoj=deoj,
                seoj=seoj,
                esv=ESV.SETC,
                properties=properties,
                response_esvs=_SET_RESPONSE_ESVS,
                request_timeout=request_timeout,
                priority=priority,
            )
        except ClientStoppedError:
            return no_response
        if not sent:
            return SetRequestResult(
                sent=False,
                response_esv=None,
                accepted_epcs=frozenset(),
                rejected_epcs=frozenset(),
                unanswered_epcs=requested_epcs,
            )
        if response_frame is None:
            return no_response

        if response_frame.esv == ESV.SET_RES:
            return SetRequestResult(
                sent=True,
                response_esv=response_frame.esv,
                accepted_epcs=requested_epcs,
                rejected_epcs=frozenset(),
                unanswered_epcs=frozenset(),
            )

        accepted_epcs: set[int] = set()
        rejected_epcs: set[int] = set()
        for prop in response_frame.properties:
            if prop.epc not in requested_epcs:
                continue
            if prop.edt:
                rejected_epcs.add(prop.epc)
            else:
                accepted_epcs.add(prop.epc)

        return SetRequestResult(
            sent=True,
            response_esv=response_frame.esv,
            accepted_epcs=frozenset(accepted_epcs),
            rejected_epcs=frozenset(rejected_epcs),
            unanswered_epcs=requested_epcs
            - frozenset(accepted_epcs)
            - frozenset(rejected_epcs),
        )

    def _on_transport_error(self, error: Exception) -> None:
        """Report an unexpected loss of the UDP transport."""
        self._dispatch(HemsErrorEvent(received_at=time.monotonic(), error=error))

    def _on_receive(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle received UDP data."""
        try:
            frame = Frame.decode(data)
        except FrameDecodeError as err:
            self._decode_error_count += 1
            _LOGGER.debug("Discarding undecodable datagram from %s: %s", addr, err)
            return
        try:
            self._process_frame(frame, addr)
        except Exception:
            _LOGGER.exception(
                "Error processing ECHONET Lite frame from %s: %s",
                addr,
                _format_frame(frame),
            )

    def _process_frame(self, frame: Frame, addr: tuple[str, int]) -> None:
        """Route a decoded frame to pending requests and subscribers."""
        address = addr[0]
        _LOGGER.debug(
            "Received ECHONET Lite frame from %s:%d: %s",
            addr[0],
            addr[1],
            _format_frame(frame),
        )

        # Discard request ESVs - loopback or other node requests
        if frame.esv in {
            ESV.SETI,
            ESV.SETC,
            ESV.GET,
            ESV.INF_REQ,
            ESV.SETGET,
        }:
            _LOGGER.debug("Discarding request frame: %s", _format_frame(frame))
            return

        # Responses also continue to be dispatched as events below; a 0x73
        # answering an INF_REQ also carries a real notification.
        pending = self._pending.get(frame.tid)
        if (
            pending is not None
            and frame.esv in pending.response_esvs
            and self._matches_request(
                frame, address, pending.address, pending.deoj, pending.seoj
            )
        ):
            del self._pending[frame.tid]
            if not pending.future.done():
                pending.future.set_result(frame)

        # An INF from an unknown address is normally a multicast state
        # update. Discover that source directly before applying any
        # device-object notification it carried.
        if (
            frame.esv == ESV.INF
            and address not in self._device_addresses
            and frame.seoj.class_code != NODE_PROFILE_CLASS
        ):
            self._queue_unknown_frame(frame, address)
            return

        # Handle node profile responses (identification and instance list)
        if frame.seoj.class_code == NODE_PROFILE_CLASS:
            self._handle_node_profile(frame, address)
            return

        if frame.esv == ESV.INFC:
            _LOGGER.debug(
                "Received INFC (0x74) from %s (EOJ: %r), sending INFC_RES",
                address,
                frame.seoj,
            )
            infc_res = Frame(
                tid=frame.tid,
                seoj=frame.deoj,
                deoj=frame.seoj,
                esv=ESV.INFC_RES,
                properties=[Property(epc=prop.epc) for prop in frame.properties],
            )
            self._send_to_address(infc_res, address)

        node_id = self._device_addresses.get(address)
        if not node_id:
            self._queue_unknown_frame(frame, address)
            return

        self._dispatch(
            HemsFrameEvent(
                received_at=time.monotonic(),
                frame=frame,
                node_id=node_id,
                eoj=frame.seoj,
            )
        )

    @staticmethod
    def _matches_request(
        frame: Frame, address: str, req_address: str, req_deoj: EOJ, req_seoj: EOJ
    ) -> bool:
        """Return whether a response frame answers a pending request."""
        if address == req_address and frame.seoj == req_deoj and frame.deoj == req_seoj:
            _LOGGER.debug(
                "Matched pending request from %s: %s", address, _format_frame(frame)
            )
            return True
        _LOGGER.debug(
            "Ignoring response with a pending TID from %s (expected %s %r): %s",
            address,
            req_address,
            req_deoj,
            _format_frame(frame),
        )
        return False

    def _queue_unknown_frame(self, frame: Frame, address: str) -> None:
        """Queue a frame from an unknown address and discover that address."""
        pending_frames = self._pending_frames.get(address)
        if pending_frames is None:
            if len(self._pending_frames) >= _MAX_PENDING_ADDRESSES:
                _LOGGER.debug(
                    "Dropping frame from unknown device at %s: too many unknown "
                    "addresses",
                    address,
                )
                return
            pending_frames = self._pending_frames[address] = []
        elif len(pending_frames) >= _MAX_PENDING_FRAMES_PER_ADDRESS:
            del pending_frames[0]
        _LOGGER.debug(
            "Queuing frame from unknown device at %s (EOJ: %r)", address, frame.seoj
        )
        pending_frames.append((frame, frame.seoj, time.monotonic()))
        self._schedule_address_discovery(address)

    def _handle_node_profile(self, frame: Frame, address: str) -> None:
        """Handle node profile responses."""
        # Extract node_id and instances using shared logic
        node_id, instances = _extract_discovery_info(frame)

        if node_id:
            previous_node_id = self._device_addresses.get(address)
            if previous_node_id and previous_node_id != node_id:
                self._clear_get_capabilities_for_node(previous_node_id)
            self._device_addresses.forceput(address, node_id)
        else:
            node_id = self._device_addresses.get(address)

        if not node_id:
            # A recurring D6 response, or a D5 notification, does not carry
            # enough information to identify an unknown node. Resolve it with
            # the same direct discovery used for unknown device notifications.
            self._schedule_address_discovery(address)
            return

        if not instances:
            return

        _LOGGER.debug(
            "Node profile for %s: EPCs=%s",
            node_id,
            [f"0x{p.epc:02X}" for p in frame.properties if p.edt],
        )

        self._dispatch(
            HemsInstanceListEvent(
                received_at=time.monotonic(),
                instances=instances,
                node_id=node_id,
            )
        )

        # Replay device notifications only after their instances have been
        # registered by the event consumer.
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

    def _dispatch(self, event: RuntimeEvent) -> None:
        """Dispatch an event to all subscribers."""
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception:
                _LOGGER.exception("Error in runtime event callback")

    async def _discovery_loop(self) -> None:
        """Send the recurring node probe."""
        await asyncio.sleep(_INITIAL_DISCOVERY_DELAY)
        while self._protocol:
            try:
                self.probe_nodes()
                await asyncio.sleep(self._discovery_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.exception("Error in discovery loop")

    async def _attempt_get_request(
        self,
        address: str,
        deoj: EOJ,
        seoj: EOJ,
        epcs: list[int],
        request_timeout: float,
        priority: RequestPriority,
    ) -> tuple[bool, GetResponse | None]:
        """Send one Get request with a new TID and wait for its response.

        Returns:
            Whether the request was sent, and the response or None if the
            request was not sent or timed out.

        Raises:
            ClientStoppedError: The client was stopped.
        """
        sent, response, latency = await self._send_request(
            address,
            deoj=deoj,
            seoj=seoj,
            esv=ESV.GET,
            properties=[Property(epc=epc) for epc in epcs],
            response_esvs=_GET_RESPONSE_ESVS,
            request_timeout=request_timeout,
            priority=priority,
        )
        if response is None:
            return sent, None
        return sent, GetResponse(
            esv=response.esv,
            properties=tuple(response.properties),
            latency=latency,
        )

    async def _send_request(
        self,
        address: str,
        *,
        deoj: EOJ,
        seoj: EOJ,
        esv: ESV,
        properties: list[Property],
        response_esvs: frozenset[ESV],
        request_timeout: float,
        priority: RequestPriority,
    ) -> tuple[bool, Frame | None, float]:
        """Send one request through the node's queue and wait for its response.

        ``request_timeout`` starts when the request is sent, not when it is
        queued.

        Returns:
            Whether the request was sent, the response frame or None if it
            was not sent or timed out, and the seconds from sending the
            request to receiving the response.

        Raises:
            ClientStoppedError: The client was stopped.
        """
        queue = self._node_queues.get(address)
        if queue is None:
            queue = self._node_queues[address] = _NodeRequestQueue()
        await queue.acquire(priority)
        tid: int | None = None
        try:
            tid = self._next_transaction_id()
            future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
            self._pending[tid] = _PendingRequest(
                address=address,
                deoj=deoj,
                seoj=seoj,
                response_esvs=response_esvs,
                future=future,
            )
            frame = Frame(tid=tid, seoj=seoj, deoj=deoj, esv=esv, properties=properties)
            sent_at = time.monotonic()
            if not self._send_to_address(frame, address):
                return False, None, 0.0
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(future), request_timeout
                )
            except TimeoutError:
                if not future.done():
                    _LOGGER.debug(
                        "Request to %s timed out: %s", address, _format_frame(frame)
                    )
                    return True, None, time.monotonic() - sent_at
                response = future.result()
            return True, response, time.monotonic() - sent_at
        finally:
            if tid is not None:
                self._pending.pop(tid, None)
            queue.release()
            if queue.idle and self._node_queues.get(address) is queue:
                del self._node_queues[address]

    def _send_to_address(self, frame: Frame, address: str) -> bool:
        """Send a frame to a specific address.

        Args:
            frame: Frame to send.
            address: Target IP address.

        Returns:
            True if sent successfully.

        """
        if not self._protocol:
            return False

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
