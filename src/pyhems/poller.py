"""Property polling scheduler for ECHONET Lite devices."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection
from dataclasses import dataclass

from .const import ESV
from .device_manager import DeviceManager
from .get_batch_policy import take_first_batch

_LOGGER = logging.getLogger(__name__)

# Latency EWMA smoothing factor: weight given to the newest observation.
_LATENCY_EWMA_ALPHA = 0.3
# Exponential backoff base for consecutive unanswered polls.
_BACKOFF_BASE = 2.0
# Cap on the backoff exponent to avoid pathological float growth; the actual
# interval is separately capped by ``max_interval``.
_MAX_BACKOFF_EXPONENT = 10
# Default safety margin applied to observed latency when computing the
# adaptive interval (see PropertyPoller.__init__).
_DEFAULT_SAFETY_FACTOR = 2.5
# Default ceiling for the adaptive interval (seconds).
_DEFAULT_MAX_INTERVAL = 600.0
# Default base interval for normal polling (seconds).
_DEFAULT_POLL_INTERVAL = 60.0
# Default base interval for fast polling (seconds).
_DEFAULT_FAST_POLL_INTERVAL = 10.0


@dataclass
class _DeviceScheduleState:
    """Per-device state for the adaptive polling algorithm.

    Consolidates everything :class:`PropertyPoller` tracks per device (in
    addition to ``_pending``/``_scheduled``, which are keyed the same way
    but serve a different purpose) into a single object, instead of several
    parallel dicts that all need to be kept in sync by hand.
    """

    # Monotonic timestamp when a poll was sent and a response is still
    # outstanding, or None if no poll is currently in flight. Shared by both
    # tiers: only one poll (normal or fast) may be in flight at a time.
    awaiting_since: float | None = None
    # Transaction ID of the poll currently in flight, used to ignore
    # unrelated response frames from the same device.
    awaiting_tid: int | None = None
    # Monotonic timestamp of the most recent normal-tier poll actually sent.
    last_polled_at: float | None = None
    # Same as above, but for the fast tier.
    last_fast_polled_at: float | None = None
    # Smoothed round-trip latency observed for this device, or None if no
    # observation has been made yet. Shared by both tiers, since it reflects
    # the device's actual responsiveness regardless of which tier triggered
    # the poll.
    latency_ewma: float | None = None
    # Number of consecutive polls that timed out without a response (reset
    # to 0 whenever any frame is received). Shared by both tiers for the
    # same reason as ``latency_ewma``.
    consecutive_failures: int = 0
    # Whether the poll cycle in progress is a scheduled poll used for
    # liveness tracking (immediate polls are not).
    liveness_requested: bool = False
    # EPCs still to be sent for the poll cycle currently in progress.
    remaining_epcs: tuple[int, ...] = ()
    # Whether ``remaining_epcs`` belongs to the fast tier (affects which
    # last-polled timestamp subsequent chunks update).
    remaining_epcs_fast: bool = False
    # Whether ``remaining_epcs`` belongs to the normal tier. Both flags can be
    # true when a merged normal/fast poll is split into chunks.
    remaining_epcs_normal: bool = False

    def end_cycle(self) -> None:
        """Forget the poll cycle in progress."""
        self.liveness_requested = False
        self.remaining_epcs = ()
        self.remaining_epcs_fast = False
        self.remaining_epcs_normal = False


@dataclass(frozen=True, slots=True)
class DevicePollerStats:
    """Read-only per-device snapshot of adaptive poller state."""

    normal_interval: float
    fast_interval: float
    latency_ewma: float | None
    consecutive_failures: int


class PropertyPoller:
    """Periodically poll devices whose monitored EPCs lack notification support.

    This scheduler iterates ``device_manager.data`` on a fixed interval and
    sends GET requests for each device's ``poll_epcs``.  It also supports
    expedited polling (e.g. after a Set operation) via
    :meth:`schedule_immediate_poll`.

    A device is never sent a new poll while a previous one is still
    outstanding (no response frame observed yet): this avoids piling up
    overlapping GET requests on slow devices, which would otherwise make
    them fall further behind. See :meth:`_is_awaiting`.

    Each device also gets its own *adaptive* polling interval on top of the
    shared ``poll_interval`` tick: devices with a higher observed response
    latency are polled less often (scaled by ``safety_factor``), and devices
    that repeatedly fail to answer within ``awaiting_timeout`` are backed off
    exponentially. Both are capped by ``max_interval``. See
    :meth:`_effective_interval`.

    Devices with a non-empty ``NodeState.fast_poll_epcs`` (e.g. instantaneous
    power) are additionally polled on a second, faster cadence
    by the combined scheduler. The fast tier shares the same in-flight
    tracking and latency/backoff signals as the normal tier, so a device that
    turns out to be slow automatically has its fast-tier cadence folded back
    into the normal one instead of being hammered independently. See
    :meth:`_effective_fast_interval`.

    Each poll cycle is split into chunks by the device's concurrent-response
    limit learned during setup and by device-specific GET policies. Chunks
    are sent one at a time, each only after the previous chunk's response is
    observed (see :meth:`_send_next_chunk`). The poller never probes or
    learns the limit, and never requeues EPCs missing from a response: a
    timeout ends the current cycle, which is left to the next one.

    Both tiers request ``device_manager.effective_poll_epcs()`` /
    ``effective_fast_poll_epcs()`` rather than the raw
    ``NodeState.poll_epcs``/``fast_poll_epcs``: callers can narrow the set of
    EPCs actually polled per device via ``DeviceManager.subscribe_epcs()``
    (e.g. Home Assistant unsubscribing a disabled Entity's EPC). A device
    with no active subscribers for a tier is skipped entirely for that tier.
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        *,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        awaiting_timeout: float | None = None,
        safety_factor: float = _DEFAULT_SAFETY_FACTOR,
        max_interval: float = _DEFAULT_MAX_INTERVAL,
        fast_poll_interval: float = _DEFAULT_FAST_POLL_INTERVAL,
    ) -> None:
        """Initialize the poller with a device manager and polling interval.

        Args:
            device_manager: The device manager to poll.
            poll_interval: Base interval between poll cycles (seconds). Also
                the lower bound of the per-device adaptive interval. Defaults
                to 60 seconds.
            awaiting_timeout: How long to wait for a response to an
                outstanding poll before giving up and allowing a new one to
                be sent (seconds). Defaults to ``poll_interval`` so a device
                that never answers is retried on the next regular cycle.
            safety_factor: Multiplier applied to a device's observed latency
                (EWMA) when computing its adaptive interval. Higher values
                poll slow devices more conservatively.
            max_interval: Upper bound for the per-device adaptive interval
                (seconds), regardless of observed latency or backoff.
                Defaults to 600 seconds (10 minutes).
            fast_poll_interval: Base interval for the high-frequency tier
                (seconds). Defaults to 10 seconds.
        """
        self._device_manager = device_manager
        self._poll_interval = max(1.0, float(poll_interval))
        self._awaiting_timeout = (
            self._poll_interval
            if awaiting_timeout is None
            else max(0.0, float(awaiting_timeout))
        )
        self._safety_factor = max(1.0, float(safety_factor))
        self._max_interval = max(self._poll_interval, float(max_interval))
        self._fast_poll_interval = max(1.0, float(fast_poll_interval))
        self._pending: set[str] = set()
        self._scheduled: dict[str, asyncio.TimerHandle] = {}
        self._task: asyncio.Task[None] | None = None

        # device_key -> per-device scheduling state (in-flight tracking,
        # latency EWMA, backoff, last-polled timestamps). See
        # _DeviceScheduleState.
        self._state: dict[str, _DeviceScheduleState] = {}
        self._unsub_frame_received = device_manager.on_frame_received(
            self._on_frame_received
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the periodic polling loop.

        Fast and normal polling are merged into the same scheduler, so a device
        that becomes due on both tiers is requested once with the union of EPCs.
        """
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._poll_loop(), name="pyhems_property_poller"
            )

    def stop(self) -> None:
        """Cancel the polling loop and all scheduled callbacks."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for handle in self._scheduled.values():
            handle.cancel()
        self._scheduled.clear()
        self._pending.clear()
        self._state.clear()
        self._unsub_frame_received()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def schedule_immediate_poll(self, device_key: str, *, delay: float = 1.0) -> None:
        """Schedule polling for a device earlier than the regular cadence.

        Intended to be called after a Set operation so the caller can
        observe the updated device state sooner.
        """
        if device_key not in self._device_manager.data:
            return

        if device_key in self._pending:
            return

        if self._is_awaiting(device_key):
            return

        if handle := self._scheduled.pop(device_key, None):
            handle.cancel()

        delay = max(0.0, float(delay))
        loop = asyncio.get_running_loop()
        self._scheduled[device_key] = loop.call_later(
            delay, self._scheduled_fire, device_key
        )

    def get_device_stats(self, device_key: str) -> DevicePollerStats:
        """Return a diagnostics-friendly snapshot for one device."""
        state = self._state.get(device_key)
        return DevicePollerStats(
            normal_interval=self._effective_interval(device_key),
            fast_interval=self._effective_fast_interval(device_key),
            latency_ewma=None if state is None else state.latency_ewma,
            consecutive_failures=0 if state is None else state.consecutive_failures,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Run forever, polling each device on the combined cadence.

        When the fast tier is enabled, the loop wakes on the faster tick and
        then decides whether normal and/or fast EPCs are due. This avoids two
        separate poll tasks racing on the same device while still respecting
        each tier's adaptive timing.
        """
        while True:
            await asyncio.sleep(self._fast_poll_interval)
            self._cleanup_stale()
            self._schedule_polls()

    def _cleanup_stale(self) -> None:
        """Remove pending/scheduled entries for devices no longer present."""
        current = set(self._device_manager.data)
        for device_key in list(self._pending):
            if device_key not in current:
                self._pending.discard(device_key)
        for device_key in list(self._scheduled):
            if device_key not in current:
                self._scheduled.pop(device_key).cancel()
        for device_key in list(self._state):
            if device_key not in current:
                self._state.pop(device_key, None)

    def _get_state(self, device_key: str) -> _DeviceScheduleState:
        """Return the per-device schedule state, creating it if absent."""
        state = self._state.get(device_key)
        if state is None:
            state = _DeviceScheduleState()
            self._state[device_key] = state
        return state

    def _schedule_polls(self) -> None:
        """Enqueue poll requests for devices that need polling.

        When both tiers are due for the same device, the request is merged into a
        single GET containing the union of the normal and fast EPC sets. This
        avoids the race where a slow device would otherwise receive separate
        overlapping polls on the same tick.
        """
        now = time.monotonic()
        for device_key, node in self._device_manager.data.items():
            if device_key in self._pending or device_key in self._scheduled:
                continue
            if self._is_awaiting(device_key):
                continue
            if not node.poll_epcs and not node.fast_poll_epcs:
                continue
            state = self._state.get(device_key)
            last_polled_at = state.last_polled_at if state is not None else None
            normal_due = bool(node.poll_epcs) and (
                last_polled_at is None
                or now - last_polled_at >= self._effective_interval(device_key)
            )
            last_fast_polled_at = (
                state.last_fast_polled_at if state is not None else None
            )
            fast_due = bool(node.fast_poll_epcs) and (
                last_fast_polled_at is None
                or now - last_fast_polled_at
                >= self._effective_fast_interval(device_key)
            )

            if not normal_due and not fast_due:
                continue

            self._fire_poll(device_key, fast=fast_due, normal=normal_due)

    def _scheduled_fire(self, device_key: str) -> None:
        self._scheduled.pop(device_key, None)
        self._fire_poll(device_key, normal=True, fast=True, liveness=False)

    def _fire_poll(
        self,
        device_key: str,
        *,
        fast: bool,
        normal: bool,
        liveness: bool = True,
    ) -> None:
        if device_key in self._pending:
            return
        node = self._device_manager.data.get(device_key)
        normal_epcs = (
            self._device_manager.effective_poll_epcs(device_key)
            if normal
            else frozenset()
        )
        fast_epcs = (
            self._device_manager.effective_fast_poll_epcs(device_key)
            if fast and node is not None and node.fast_poll_epcs
            else frozenset()
        )
        epcs = normal_epcs | fast_epcs
        if not epcs:
            return
        self._poll_node(
            device_key, epcs=epcs, fast=fast, normal=normal, liveness=liveness
        )

    def _is_awaiting(self, device_key: str) -> bool:
        """Return True if a poll response for ``device_key`` is still outstanding.

        If the outstanding poll has been unanswered for longer than
        ``awaiting_timeout``, the wait is abandoned (the entry is cleared),
        the rest of the poll cycle is dropped, and the timeout is counted as a
        failure, feeding the exponential backoff in :meth:`_effective_interval`.
        """
        state = self._state.get(device_key)
        if state is None or state.awaiting_since is None:
            return False
        if time.monotonic() - state.awaiting_since >= self._awaiting_timeout:
            liveness_requested = state.liveness_requested
            state.awaiting_since = None
            state.awaiting_tid = None
            state.end_cycle()
            state.consecutive_failures += 1
            if liveness_requested:
                self._device_manager.record_poll_failure(device_key)
            return False
        return True

    def _effective_interval(self, device_key: str) -> float:
        """Return the current adaptive polling interval for a device.

        Combines two independent signals, each capped by ``max_interval``:

        - Observed round-trip latency (EWMA), scaled by ``safety_factor``,
          so a consistently slow-but-responsive device is polled less often.
        - Consecutive unanswered polls, backed off exponentially, so a
          device that stops responding entirely is polled far less often.
        """
        interval = self._poll_interval

        state = self._state.get(device_key)
        if state is not None:
            if state.latency_ewma is not None:
                interval = max(interval, state.latency_ewma * self._safety_factor)

            if state.consecutive_failures:
                exponent = min(state.consecutive_failures, _MAX_BACKOFF_EXPONENT)
                interval = max(
                    interval, self._poll_interval * (_BACKOFF_BASE**exponent)
                )

        return min(interval, self._max_interval)

    def _effective_fast_interval(self, device_key: str) -> float:
        """Return the current adaptive polling interval for the fast tier.

        Computed the same way as :meth:`_effective_interval`, but using
        ``fast_poll_interval`` as the base instead of ``poll_interval``. If
        the result would exceed the device's normal-tier interval, it is
        folded down to that value instead: once a device is confirmed slow
        enough that the fast tier offers no benefit, there is no point
        polling it on a separate, independently-growing schedule.
        """
        interval = self._fast_poll_interval

        state = self._state.get(device_key)
        if state is not None:
            if state.latency_ewma is not None:
                interval = max(interval, state.latency_ewma * self._safety_factor)

            if state.consecutive_failures:
                exponent = min(state.consecutive_failures, _MAX_BACKOFF_EXPONENT)
                interval = max(
                    interval, self._fast_poll_interval * (_BACKOFF_BASE**exponent)
                )

        interval = min(interval, self._max_interval)
        return min(interval, self._effective_interval(device_key))

    def _update_latency(self, state: _DeviceScheduleState, latency: float) -> None:
        """Update the smoothed (EWMA) latency estimate for a device."""
        if state.latency_ewma is None:
            state.latency_ewma = latency
        else:
            state.latency_ewma = (
                _LATENCY_EWMA_ALPHA * latency
                + (1 - _LATENCY_EWMA_ALPHA) * state.latency_ewma
            )

    def _on_frame_received(
        self,
        device_key: str,
        tid: int,
        _esv: ESV,
        _received_epcs: frozenset[int],
    ) -> None:
        """Clear the awaiting state and send the next chunk on a matching response.

        A response (0x72 or 0x52) to the outstanding poll is evidence that the
        device is responsive, even if some EPCs are missing or empty: the
        consecutive-failure counter is reset and the observed latency feeds the
        EWMA used by :meth:`_effective_interval`. Missing EPCs are not
        requested again in this cycle.
        """
        state = self._state.get(device_key)
        if state is None or state.awaiting_tid is None or tid != state.awaiting_tid:
            return
        sent_at = state.awaiting_since
        state.awaiting_since = None
        state.awaiting_tid = None
        if sent_at is not None:
            self._update_latency(state, time.monotonic() - sent_at)
        state.consecutive_failures = 0
        if state.liveness_requested:
            self._device_manager.record_poll_success(device_key)
        if state.remaining_epcs:
            self._send_next_chunk(device_key)
        else:
            state.end_cycle()

    def _poll_node(
        self,
        device_key: str,
        *,
        epcs: Collection[int],
        fast: bool,
        normal: bool,
        liveness: bool = True,
    ) -> None:
        """Start a poll cycle for ``epcs`` and send its first chunk."""
        state = self._get_state(device_key)
        state.remaining_epcs = tuple(sorted(epcs))
        state.remaining_epcs_fast = fast
        state.remaining_epcs_normal = normal
        state.liveness_requested = liveness
        self._send_next_chunk(device_key)

    def _send_next_chunk(self, device_key: str) -> None:
        """Plan and send the next chunk of the poll cycle in progress."""
        state = self._get_state(device_key)
        node = self._device_manager.data.get(device_key)
        if node is None or not state.remaining_epcs:
            state.end_cycle()
            return
        capacity = self._device_manager.get_observed_batch_capacity(device_key)
        chunk, remaining = take_first_batch(
            state.remaining_epcs,
            manufacturer_code=node.manufacturer_code,
            class_code=node.eoj.class_code,
            observed_batch_capacity=(
                capacity if isinstance(capacity, int) and capacity >= 1 else None
            ),
        )
        if not chunk:
            state.end_cycle()
            return
        state.remaining_epcs = remaining

        self._pending.add(device_key)
        try:
            sent_tid = self._device_manager.poll_device(device_key, chunk)
        except OSError as err:
            _LOGGER.debug(
                "Failed to request properties for node %s: %s", device_key, err
            )
            sent_tid = None
        finally:
            self._pending.discard(device_key)

        if sent_tid is None:
            _LOGGER.debug(
                "Failed to poll node %s: no poll EPCs or address unknown",
                device_key,
            )
            if state.liveness_requested:
                self._device_manager.record_poll_failure(device_key)
            state.end_cycle()
            return

        now = time.monotonic()
        state.awaiting_since = now
        state.awaiting_tid = sent_tid
        if state.remaining_epcs_normal:
            state.last_polled_at = now
        if state.remaining_epcs_fast:
            state.last_fast_polled_at = now


__all__ = ["DevicePollerStats", "PropertyPoller"]
