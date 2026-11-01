"""Device management for ECHONET Lite nodes."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field

from ._definitions_generated import REGISTRY
from .const import (
    CONTROLLER_INSTANCE,
    EPC,
    ESV,
    SET_REQUEST_TIMEOUT,
)
from .eoj import EOJ
from .frame import Property
from .installation_location import (
    InstallationLocation,
    decode_installation_location,
)
from .runtime import (
    ClientStoppedError,
    GetAbortedError,
    GetResponse,
    HemsClient,
    HemsFrameEvent,
    HemsInstanceListEvent,
    RequestPriority,
    RuntimeEvent,
    SetRequestResult,
)

_LOGGER = logging.getLogger(__name__)

DeviceCallback = Callable[[str], None]
RuntimeActivityCallback = Callable[[float], None]
# EPCs per device class: a mapping of class_code -> EPCs, or a function of
# (class_code, manufacturer_code) for manufacturer-specific EPCs.
EpcSelection = Mapping[int, frozenset[int]] | Callable[[int, int], Collection[int]]

# Frame ESVs whose EDTs carry current property values.
_VALUE_ESVS = frozenset({ESV.GET_RES, ESV.GET_SNA, ESV.INF, ESV.INFC})
# Frames received for a device object while it is being set up.
_MAX_DEFERRED_FRAMES = 64


def _select_epcs(
    selection: EpcSelection, class_code: int, manufacturer_code: int
) -> frozenset[int]:
    """Return the EPCs selected for a device class and manufacturer."""
    if callable(selection):
        return frozenset(selection(class_code, manufacturer_code))
    return selection.get(class_code, frozenset())


def _parse_property_map(edt: bytes) -> frozenset[int] | None:
    """Parse an ECHONET Lite property map EDT (0x9D/0x9E/0x9F).

    Returns None if the EDT is empty or malformed. A valid map listing no
    EPCs (e.g. ``EDT=00``) returns an empty set.

    Property maps have two formats:
    - List format (count <= 15): [count, epc1, epc2, ...]
    - Bitmap format (count >= 16): [count, 16 bytes for EPCs 0x80-0xFF]

    In bitmap format, each bit represents whether an EPC is present.
    The mapping follows the ECHONET Lite specification:
    - Byte index (0-15) = EPC low nibble (0x80, 0x81, ..., 0x8F)
    - Bit index (0-7) = EPC high nibble offset (bit0=0x8x, bit1=0x9x, ..., bit7=0xFx)
    """
    if not edt:
        return None

    count = edt[0]

    if count <= 15:
        # List format: EPCs are enumerated directly
        if len(edt) < count + 1:
            _LOGGER.debug(
                "Property map list too short: expected %d EPCs, got %d bytes",
                count,
                len(edt) - 1,
            )
            return None
        return frozenset(edt[1 : count + 1])

    # Bitmap format: 16 bytes representing EPCs 0x80-0xFF
    if len(edt) < 17:
        _LOGGER.debug(
            "Property map bitmap too short: expected 17 bytes, got %d", len(edt)
        )
        return None

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
) -> tuple[frozenset[int] | None, frozenset[int] | None, frozenset[int] | None]:
    """Extract and parse Get/Set/Inf property maps from property values.

    Each map is None if it is missing or malformed.
    """
    get_epcs = _parse_property_map(properties.get(EPC.GET_PROPERTY_MAP, b""))
    set_epcs = _parse_property_map(properties.get(EPC.SET_PROPERTY_MAP, b""))
    inf_epcs = _parse_property_map(properties.get(EPC.INF_PROPERTY_MAP, b""))
    return get_epcs, set_epcs, inf_epcs


def _compute_poll_epcs(
    *,
    monitored_epcs: frozenset[int],
    get_epcs: frozenset[int],
    fast_candidate_epcs: frozenset[int],
    confirmed_inf_epcs: frozenset[int],
) -> tuple[frozenset[int], frozenset[int]]:
    """Split monitored, gettable EPCs into normal and fast poll tiers.

    An EPC is only excluded from polling once its INF_REQ (0x63)
    subscription is *confirmed* successful (``confirmed_inf_epcs``); EPCs
    that are merely candidates (in the device's INF property map) but not
    yet confirmed, rejected (0x53), or unanswered, remain polled. This is
    re-run whenever ``confirmed_inf_epcs`` changes (see
    :meth:`DeviceManager._send_initial_notification`).

    Returns:
        ``(poll_epcs, fast_poll_epcs)``, a partition of the pollable EPCs.
    """
    pollable = (monitored_epcs & get_epcs) - confirmed_inf_epcs
    fast_poll_epcs = pollable & fast_candidate_epcs
    poll_epcs = pollable - fast_poll_epcs
    return poll_epcs, fast_poll_epcs


def _extract_node_profile_info(
    properties: Mapping[int, bytes],
) -> tuple[int | None, str | None, str | None]:
    """Extract manufacturer code, product code, and serial number."""
    manufacturer_code: int | None = None
    if (edt := properties.get(EPC.MANUFACTURER_CODE)) and len(edt) >= 3:
        manufacturer_code = int.from_bytes(edt[:3], "big")

    product_code = _decode_ascii_property(properties.get(EPC.PRODUCT_CODE, b""))
    serial_number = _decode_ascii_property(properties.get(EPC.SERIAL_NUMBER, b""))
    return manufacturer_code, product_code, serial_number


def _device_key(node_id: str, eoj: EOJ) -> str:
    """Return the unique key for a node object."""
    return f"{node_id}-{eoj:06x}"


def _collection_range_recovery_properties(
    node: NodeState,
) -> list[Property]:
    """Build one-shot range recovery properties for unset lists."""
    properties: list[Property] = []
    for binding in REGISTRY.collection_bindings.get(node.eoj.class_code, ()):
        if (
            binding.selector_epc is None
            or binding.count_epc is None
            or binding.max_range is None
            or binding.selector_epc in node.range_recovery_attempted_epcs
        ):
            continue

        result_edt = node.properties.get(binding.result_epc)
        if result_edt is None or len(result_edt) < 2 or 0xFD not in result_edt[:2]:
            continue

        count_edt = node.properties.get(binding.count_epc)
        if count_edt is None or len(count_edt) != 1:
            continue
        channel_count = count_edt[0]
        if not 1 <= channel_count <= 252:
            continue

        range_value = min(binding.max_range, channel_count)
        if not 1 <= range_value <= 0xFF:
            continue

        properties.append(
            Property(
                epc=binding.selector_epc,
                edt=bytes((1, range_value)),
            )
        )

    return properties


@dataclass(slots=True)
class NodeState:
    """State for a discovered ECHONET Lite node."""

    eoj: EOJ
    properties: dict[int, bytes]
    node_id: str
    manufacturer_code: int
    get_epcs: frozenset[int]
    set_epcs: frozenset[int]
    inf_epcs: frozenset[int]
    poll_epcs: frozenset[int]
    fast_poll_epcs: frozenset[int]
    product_code: str | None
    serial_number: str | None
    # Monitored EPCs for this device's class (candidate set for both Get
    # polling and INF_REQ subscription).
    monitored_epcs: frozenset[int] = field(default_factory=frozenset)
    # Subset of monitored_epcs earmarked for the high-frequency poll tier
    # (see DeviceManager's fast_epcs). Static: does not change once set.
    fast_candidate_epcs: frozenset[int] = field(default_factory=frozenset)
    # Subset of monitored_epcs for which an INF_REQ (0x63) subscription was
    # attempted (monitored_epcs & inf_epcs at setup time).
    attempted_inf_epcs: frozenset[int] = field(default_factory=frozenset)
    # Subset of attempted_inf_epcs confirmed via a successful 0x73 response:
    # these are excluded from poll_epcs/fast_poll_epcs since notifications
    # are expected instead.
    confirmed_inf_epcs: frozenset[int] = field(default_factory=frozenset)
    # Subset of attempted_inf_epcs that failed to subscribe (0x53, or no
    # response before the request timed out). Kept polled like any other
    # non-confirmed EPC; tracked separately for diagnostics visibility.
    failed_inf_epcs: frozenset[int] = field(default_factory=frozenset)
    # Whether the most recent scheduled poll received a response. None means
    # no pollable EPCs exist or no poll has completed yet.
    polling_available: bool | None = None
    # Range-selection EPCs for collection properties that have already
    # received a one-shot recovery attempt during this runtime.
    range_recovery_attempted_epcs: set[int] = field(default_factory=set)

    @property
    def device_key(self) -> str:
        """Return the unique device key."""
        return _device_key(self.node_id, self.eoj)

    @property
    def manufacturer_name_en(self) -> str | None:
        """Return the English manufacturer name, if known."""
        manufacturer = REGISTRY.manufacturers.get(self.manufacturer_code)
        return manufacturer.name_en if manufacturer else None

    @property
    def manufacturer_name_ja(self) -> str | None:
        """Return the Japanese manufacturer name, if known."""
        manufacturer = REGISTRY.manufacturers.get(self.manufacturer_code)
        return manufacturer.name_ja if manufacturer else None

    @property
    def manufacturer_name(self) -> str:
        """Manufacturer display name.

        Returns the English manufacturer name when known, otherwise the
        hexadecimal manufacturer code such as ``"0xABCDEF"``.
        """
        return self.manufacturer_name_en or f"0x{self.manufacturer_code:06X}"

    @property
    def class_name_en(self) -> str | None:
        """Return the English device class name, if known."""
        device = REGISTRY.devices.get(self.eoj.class_code)
        return device.name_en if device else None

    @property
    def class_name_ja(self) -> str | None:
        """Return the Japanese device class name, if known."""
        device = REGISTRY.devices.get(self.eoj.class_code)
        return device.name_ja if device else None

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
            self.properties.get(EPC.INSTALLATION_LOCATION)
        )


class DeviceManager:
    """Manage discovered ECHONET Lite devices.

    Handles device discovery, property tracking, and frame processing.
    This is a protocol-level manager that does not depend on Home Assistant.
    """

    def __init__(
        self,
        client: HemsClient,
        monitored_epcs: EpcSelection,
        class_code_filter: frozenset[int] | None = None,
        fast_epcs: EpcSelection | None = None,
    ) -> None:
        """Initialize the device manager.

        Args:
            client: HEMS runtime client for communication.
            monitored_epcs: EPCs to monitor per device class, either as a
                mapping of class_code -> EPCs or as a function of
                (class_code, manufacturer_code). The manufacturer code is
                read before the other EPCs during setup (ADR-0011).
            class_code_filter: If set, only these class codes are accepted.
                If None, all class codes are accepted.
            fast_epcs: EPCs that should be polled at a higher frequency (e.g.
                instantaneous power), in the same form as ``monitored_epcs``.
                EPCs that are not monitored are ignored. Defaults to no
                fast-poll EPCs for any class.
        """
        self._client = client
        self._monitored_epcs = monitored_epcs
        self._class_code_filter = class_code_filter
        self._fast_epcs = fast_epcs or {}

        self.data: dict[str, NodeState] = {}
        self.last_frame_received_at: float | None = None

        self._pending_setups: set[str] = set()
        # Per-node jobs (setup, INF_REQ, range initialization) and the task
        # running them; nodes run in parallel, jobs of one node in order.
        self._node_jobs: dict[str, deque[Callable[[], Awaitable[object]]]] = {}
        self._node_workers: dict[str, asyncio.Task[None]] = {}
        self._deferred_frames: dict[str, list[HemsFrameEvent]] = {}
        self._event_queue: asyncio.Queue[HemsFrameEvent | HemsInstanceListEvent] = (
            asyncio.Queue()
        )
        self._event_consumer_task: asyncio.Task[None] | None = None
        self._unsubscribe_runtime: Callable[[], None] | None = None

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
        self._on_runtime_activity: list[RuntimeActivityCallback] = []

    async def async_start(self) -> None:
        """Subscribe to runtime events and start serialized event processing."""
        if self._event_consumer_task is not None:
            return

        self._event_queue = asyncio.Queue()
        self._unsubscribe_runtime = self._client.subscribe(self._handle_runtime_event)
        self._event_consumer_task = asyncio.create_task(
            self._consume_runtime_events(),
            name="pyhems_device_manager_event_consumer",
        )

    async def async_stop(self) -> None:
        """Unsubscribe from runtime events and stop event processing."""
        if self._unsubscribe_runtime is not None:
            self._unsubscribe_runtime()
            self._unsubscribe_runtime = None

        workers = list(self._node_workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._node_workers.clear()
        self._node_jobs.clear()
        self._deferred_frames.clear()

        if self._event_consumer_task is None:
            return

        self._event_consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._event_consumer_task
        self._event_consumer_task = None
        self._event_queue = asyncio.Queue()

    @property
    def event_consumer_task_done(self) -> bool:
        """Return whether the event consumer is stopped or has completed."""
        return self._event_consumer_task is None or self._event_consumer_task.done()

    def on_runtime_activity(
        self, callback: RuntimeActivityCallback
    ) -> Callable[[], None]:
        """Register a callback for every received runtime event timestamp."""
        self._on_runtime_activity.append(callback)

        def unsub() -> None:
            if callback in self._on_runtime_activity:
                self._on_runtime_activity.remove(callback)

        return unsub

    def _record_runtime_activity(self, timestamp: float) -> None:
        """Notify listeners that a runtime event was received."""
        for callback in self._on_runtime_activity:
            callback(timestamp)

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

    def _notify_device_updated(self, device_key: str) -> None:
        """Notify listeners that a device's cached properties changed."""
        for updated_cb in self._on_device_updated:
            updated_cb(device_key)

    @staticmethod
    def _apply_accepted_properties(
        node: NodeState,
        properties: list[Property],
        accepted_epcs: frozenset[int],
    ) -> bool:
        """Apply requested EDTs only for EPCs accepted by the device."""
        updated = False
        for prop in properties:
            if prop.epc in accepted_epcs and node.properties.get(prop.epc) != prop.edt:
                node.properties[prop.epc] = prop.edt
                updated = True
        return updated

    async def set_properties(
        self,
        node_id: str,
        deoj: EOJ,
        properties: list[Property],
        seoj: EOJ = CONTROLLER_INSTANCE,
        request_timeout: float = SET_REQUEST_TIMEOUT,
    ) -> SetRequestResult:
        """Set device properties and cache values accepted by the device.

        Args:
            node_id: Device node ID.
            deoj: Destination EOJ.
            properties: Properties to write.
            seoj: Source EOJ.
            request_timeout: Timeout in seconds for the response.

        Returns:
            The SetC result from the runtime client.
        """
        result = await self._client.set_properties(
            node_id=node_id,
            deoj=deoj,
            properties=properties,
            seoj=seoj,
            request_timeout=request_timeout,
        )
        node = self.data.get(_device_key(node_id, deoj))
        if (
            node is not None
            and result.sent
            and self._apply_accepted_properties(node, properties, result.accepted_epcs)
        ):
            self._notify_device_updated(node.device_key)
        return result

    def _handle_runtime_event(self, event: RuntimeEvent) -> None:
        """Queue device events for serialized processing."""
        if isinstance(event, (HemsFrameEvent, HemsInstanceListEvent)):
            self._event_queue.put_nowait(event)

    async def _consume_runtime_events(self) -> None:
        """Process device events in the order received from the runtime client.

        Requests to devices are never awaited here (ADR-0011): setup runs in
        the node's job task.
        """
        while True:
            event = await self._event_queue.get()
            try:
                if isinstance(event, HemsFrameEvent):
                    await self.process_frame_event(event)
                else:
                    _LOGGER.debug(
                        "Runtime event: HemsInstanceListEvent from %s with %d instances",
                        event.node_id,
                        len(event.instances),
                    )
                    self._record_runtime_activity(event.received_at)
                    self._enqueue_node_job(
                        event.node_id,
                        functools.partial(self.process_instance_list_event, event),
                    )
            except Exception:
                _LOGGER.exception(
                    "Failed to process ECHONET Lite runtime event: %r", event
                )
            finally:
                self._event_queue.task_done()

    def _enqueue_node_job(
        self, node_id: str, job: Callable[[], Awaitable[object]]
    ) -> None:
        """Run ``job`` after the node's earlier jobs, in the node's job task."""
        self._node_jobs.setdefault(node_id, deque()).append(job)
        if node_id not in self._node_workers:
            self._node_workers[node_id] = asyncio.create_task(
                self._run_node_jobs(node_id), name=f"pyhems_node_jobs_{node_id}"
            )

    async def _run_node_jobs(self, node_id: str) -> None:
        """Run the queued jobs of one node in order."""
        jobs = self._node_jobs[node_id]
        try:
            while jobs:
                job = jobs.popleft()
                try:
                    await job()
                except Exception:
                    _LOGGER.exception("Failed to run a job for node %s", node_id)
        finally:
            self._node_jobs.pop(node_id, None)
            self._node_workers.pop(node_id, None)
            for device_key in [
                key
                for key, events in self._deferred_frames.items()
                if events[0].node_id == node_id and key not in self.data
            ]:
                del self._deferred_frames[device_key]

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

    def is_device_polling_available(self, device_key: str) -> bool | None:
        """Return the latest polling state for a device."""
        node = self.data.get(device_key)
        return None if node is None else node.polling_available

    def record_poll_success(self, device_key: str) -> None:
        """Record a successful scheduled polling response."""
        node = self.data.get(device_key)
        if node is not None:
            node.polling_available = True

    def record_poll_failure(self, device_key: str) -> None:
        """Record an unanswered scheduled polling request."""
        node = self.data.get(device_key)
        if node is not None:
            node.polling_available = False

    def get_observed_batch_capacity(self, device_key: str) -> int | None:
        """Return the concurrent GET response limit learned during setup."""
        node = self.data.get(device_key)
        if node is None:
            return None
        return self._client.get_observed_batch_capacity(node.node_id, node.eoj)

    def _effective_epcs(
        self,
        device_key: str,
        candidate_epcs: frozenset[int],
    ) -> frozenset[int]:
        if device_key not in self._subscription_confirmed:
            return candidate_epcs
        subscribed = frozenset(self._subscribed_epcs.get(device_key, ()))
        return candidate_epcs & subscribed

    async def _recover_uninitialized_collection_ranges(
        self,
        node: NodeState,
    ) -> bool:
        """Set unset collection ranges once and log the acknowledged result."""
        properties = _collection_range_recovery_properties(node)
        if not properties:
            return False

        node.range_recovery_attempted_epcs.update(prop.epc for prop in properties)
        _LOGGER.info(
            "Initializing unset collection ranges for %s: %s",
            node.device_key,
            " ".join(f"0x{prop.epc:02X}={prop.edt.hex()}" for prop in properties),
        )
        result = await self._client.set_properties(
            node_id=node.node_id,
            deoj=node.eoj,
            properties=properties,
            priority=RequestPriority.SETUP,
        )
        updated = result.sent and self._apply_accepted_properties(
            node, properties, result.accepted_epcs
        )
        if not result.sent:
            _LOGGER.warning(
                "Failed to send collection range initialization for %s; "
                "no retry will be attempted",
                node.device_key,
            )
        elif result.rejected_epcs or result.unanswered_epcs:
            _LOGGER.warning(
                "Collection range initialization for %s was not fully accepted: "
                "rejected=[%s] unanswered=[%s]",
                node.device_key,
                " ".join(f"{epc:02X}" for epc in sorted(result.rejected_epcs)),
                " ".join(f"{epc:02X}" for epc in sorted(result.unanswered_epcs)),
            )
        return updated

    async def _recover_and_notify(self, node: NodeState) -> None:
        """Initialize unset collection ranges and notify accepted values."""
        if await self._recover_uninitialized_collection_ranges(node):
            self._notify_device_updated(node.device_key)

    async def process_frame_event(self, event: HemsFrameEvent) -> bool:
        """Process a received frame and update device state.

        Frames for a device object whose node is still being set up are kept
        and applied once the setup completes. Requests triggered by the frame
        (collection range initialization) run in the node's job task.

        Args:
            event: Frame event from the runtime client.

        Returns:
            True if any device state was updated.
        """
        frame = event.frame
        eoj = event.eoj
        node_id = event.node_id

        if not frame.is_response_or_notification():
            return False

        self._record_runtime_activity(event.received_at)

        device_key = _device_key(node_id, eoj)
        existing = self.data.get(device_key)

        if existing is None:
            if node_id in self._node_workers:
                deferred = self._deferred_frames.setdefault(device_key, [])
                if len(deferred) < _MAX_DEFERRED_FRAMES:
                    deferred.append(event)
                return False
            _LOGGER.debug(
                "Ignoring frame for unknown node %s %r (setup handled elsewhere)",
                device_key,
                eoj,
            )
            return False

        return self._apply_frame(existing, event)

    def _apply_frame(self, node: NodeState, event: HemsFrameEvent) -> bool:
        """Apply the property values of a frame to a known device object."""
        frame = event.frame
        device_key = node.device_key
        self.last_frame_received_at = event.received_at

        _LOGGER.debug(
            "Received frame for %s (ESV=0x%02X): %r",
            device_key,
            frame.esv,
            frame.properties,
        )

        # Set responses, rejected notification requests and other frames
        # without current values must not overwrite cached values.
        if frame.esv not in _VALUE_ESVS:
            return False

        updated = False
        for prop in frame.properties:
            current = node.properties.get(prop.epc)
            if current is None or current != prop.edt:
                node.properties[prop.epc] = prop.edt
                updated = True

        if _collection_range_recovery_properties(node):
            self._enqueue_node_job(
                node.node_id, functools.partial(self._recover_and_notify, node)
            )

        if updated:
            self._notify_device_updated(device_key)

        return updated

    def _apply_deferred_frames(self, node: NodeState) -> None:
        """Apply frames received while the device object was being set up."""
        for event in self._deferred_frames.pop(node.device_key, ()):
            self._apply_frame(node, event)

    async def process_instance_list_event(
        self, event: HemsInstanceListEvent
    ) -> list[str]:
        """Process instance list and set up newly discovered devices.

        Args:
            event: Instance list event from the runtime client.

        Returns:
            List of device_keys for newly set up devices.
        """
        self._record_runtime_activity(event.received_at)
        node_id = event.node_id

        new_device_keys: list[str] = []
        for eoj in event.instances:
            device_key = _device_key(node_id, eoj)
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

        Setup is complete only when a valid manufacturer code (0x8A) and valid
        property maps (0x9D/0x9E/0x9F) are read. Otherwise the device is not
        registered, so it is set up again when it next appears in an
        instance list.

        Args:
            node_id: Device node ID.
            eoj: ECHONET object instance.

        Returns:
            True if setup was successful.
        """
        device_key = _device_key(node_id, eoj)
        try:
            return await self._setup_device(node_id, eoj, device_key)
        except GetAbortedError as err:
            _LOGGER.warning(
                "Setup of %s was not completed and will be retried on the next "
                "discovery: %s",
                device_key,
                err,
            )
            return False
        except ClientStoppedError:
            _LOGGER.debug("Setup of %s was interrupted by the client stop", device_key)
            return False
        except Exception:
            _LOGGER.exception("Failed to request property maps for %r", eoj)
            return False
        finally:
            self._pending_setups.discard(device_key)

    async def _setup_device(self, node_id: str, eoj: EOJ, device_key: str) -> bool:
        # The monitored EPCs depend on the manufacturer (ADR-0011).
        manufacturer_code = await self._client.read_manufacturer_code(node_id, eoj)
        base_epcs = [
            EPC.INF_PROPERTY_MAP,
            EPC.SET_PROPERTY_MAP,
            EPC.GET_PROPERTY_MAP,
            EPC.PRODUCT_CODE,
            EPC.SERIAL_NUMBER,
        ]

        initial_epcs = _select_epcs(
            self._monitored_epcs, eoj.class_code, manufacturer_code
        )
        monitored_epcs = initial_epcs - set(base_epcs) - {EPC.MANUFACTURER_CODE}
        all_epcs = base_epcs + sorted(monitored_epcs)

        _LOGGER.debug(
            "Requesting property maps for node %s %r: base=[%s], monitored=[%s]",
            node_id,
            eoj,
            " ".join(f"{epc:02X}" for epc in base_epcs),
            " ".join(f"{epc:02X}" for epc in sorted(monitored_epcs)),
        )

        response_props = await self._client.setup_get(node_id, eoj, all_epcs)
        properties: dict[int, bytes] = {
            prop.epc: prop.edt for prop in response_props if prop.edt
        }
        properties[EPC.MANUFACTURER_CODE] = manufacturer_code.to_bytes(3, "big")

        timestamp = time.monotonic()
        get_epcs, set_epcs, inf_epcs = _extract_property_maps(properties)
        _manufacturer_code, product_code, serial_number = _extract_node_profile_info(
            properties
        )

        if get_epcs is None or set_epcs is None or inf_epcs is None:
            invalid_epcs = [
                epc
                for epc, value in (
                    (EPC.INF_PROPERTY_MAP, inf_epcs),
                    (EPC.SET_PROPERTY_MAP, set_epcs),
                    (EPC.GET_PROPERTY_MAP, get_epcs),
                )
                if value is None
            ]
            _LOGGER.warning(
                "Setup of %s was not completed and will be retried on the next "
                "discovery: missing or invalid EPCs [%s]",
                device_key,
                " ".join(f"{epc:02X}" for epc in invalid_epcs),
            )
            return False

        fast_candidate_epcs = initial_epcs & _select_epcs(
            self._fast_epcs, eoj.class_code, manufacturer_code
        )
        attempted_inf_epcs = frozenset(initial_epcs & inf_epcs)
        poll_epcs, fast_poll_epcs = _compute_poll_epcs(
            monitored_epcs=initial_epcs,
            get_epcs=get_epcs,
            fast_candidate_epcs=fast_candidate_epcs,
            confirmed_inf_epcs=frozenset(),
        )

        node = NodeState(
            eoj=eoj,
            properties=properties,
            node_id=node_id,
            get_epcs=get_epcs,
            set_epcs=set_epcs,
            inf_epcs=inf_epcs,
            poll_epcs=poll_epcs,
            fast_poll_epcs=fast_poll_epcs,
            monitored_epcs=initial_epcs,
            fast_candidate_epcs=fast_candidate_epcs,
            attempted_inf_epcs=attempted_inf_epcs,
            manufacturer_code=manufacturer_code,
            product_code=product_code,
            serial_number=serial_number,
            polling_available=None,
        )

        self.last_frame_received_at = timestamp
        self.data[device_key] = node
        self._apply_deferred_frames(node)

        await self._recover_uninitialized_collection_ranges(node)
        await self._send_initial_notification(device_key, node)

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

    async def _send_initial_notification(
        self, device_key: str, node: NodeState
    ) -> None:
        """Send a one-time INF_REQ and confirm success via the 0x73/0x53 response.

        Updates ``node.confirmed_inf_epcs``/``node.failed_inf_epcs`` from the
        result, and recomputes ``node.poll_epcs``/``node.fast_poll_epcs`` so
        EPCs that are not confirmed (rejected, unanswered, or the request
        itself failed to send) fall back to being polled instead of relying
        on a notification that may never arrive.
        """
        epcs = node.attempted_inf_epcs
        if not epcs:
            return

        _LOGGER.debug(
            "Sending initial 0x63 notification request to node %s for EPCs: [%s]",
            device_key,
            " ".join(f"{epc:02X}" for epc in sorted(epcs)),
        )

        result = await self._client.request_notifications(
            node.node_id, node.eoj, sorted(epcs)
        )

        node.confirmed_inf_epcs = result.successful_epcs
        node.failed_inf_epcs = result.failed_epcs | result.unanswered_epcs
        node.poll_epcs, node.fast_poll_epcs = _compute_poll_epcs(
            monitored_epcs=node.monitored_epcs,
            get_epcs=node.get_epcs,
            fast_candidate_epcs=node.fast_candidate_epcs,
            confirmed_inf_epcs=node.confirmed_inf_epcs,
        )

        _LOGGER.debug(
            "Initial 0x63 notification request to node %s: confirmed=[%s] "
            "failed=[%s] unanswered=[%s]",
            device_key,
            " ".join(f"{epc:02X}" for epc in sorted(result.successful_epcs)),
            " ".join(f"{epc:02X}" for epc in sorted(result.failed_epcs)),
            " ".join(f"{epc:02X}" for epc in sorted(result.unanswered_epcs)),
        )

    async def poll_device(
        self,
        device_key: str,
        epcs: Collection[int] | None = None,
        *,
        request_timeout: float,
    ) -> GetResponse | None:
        """Send one GET request for a device's poll EPCs and wait for it.

        The request goes through the node's request queue with polling
        priority (ADR-0011). Values in the response are applied when the
        response frame is processed as a runtime event.

        Args:
            device_key: The device key to poll.
            epcs: EPCs to request. Defaults to the device's normal-tier
                ``poll_epcs``.
            request_timeout: Seconds to wait for the response after sending.

        Returns:
            The response, or None if the device is unknown, there is nothing
            to poll, or the request was not sent or answered.
        """
        node = self.data.get(device_key)
        if not node:
            return None
        target_epcs = node.poll_epcs if epcs is None else epcs
        if not target_epcs:
            return None

        ordered_epcs = list(target_epcs)
        _LOGGER.debug(
            "Sending 0x62 poll to node %s for EPCs: [%s]",
            device_key,
            " ".join(f"{epc:02X}" for epc in ordered_epcs),
        )
        return await self._client.request_get(
            node.node_id,
            node.eoj,
            ordered_epcs,
            request_timeout=request_timeout,
            priority=RequestPriority.POLL,
        )


__all__ = ["DeviceManager", "NodeState"]
