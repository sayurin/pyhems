"""Property polling scheduler for ECHONET Lite devices."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .device_manager import DeviceManager

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


def _chunk_epcs(epcs: frozenset[int], size: int) -> list[frozenset[int]]:
    """Split ``epcs`` into ordered chunks of at most ``size`` EPCs each."""
    ordered = sorted(epcs)
    return [frozenset(ordered[i : i + size]) for i in range(0, len(ordered), size)]


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
    # Upper bound on the number of EPCs requested in a single GET, learned
    # during setup or from observed partial responses. None means "no observed
    # limit" (request the full target EPC set in one frame).
    observed_batch_capacity: int | None = None
    # EPCs requested by the most recently sent (still in-flight) poll, or
    # None if that poll was sent without explicit tracking (e.g. an
    # immediate poll after a Set) and partial-response detection does not
    # apply to it.
    requested_epcs: frozenset[int] | None = None
    # Remaining chunks still to be sent for the poll cycle currently in
    # progress (populated when the target EPC set exceeds
    # observed_batch_capacity). Sent one at a time, each only after the
    # previous chunk's response (or timeout) is observed.
    pending_chunks: list[frozenset[int]] = field(default_factory=list)
    # Whether ``pending_chunks`` belongs to the fast tier (affects which
    # last-polled timestamp subsequent chunks update).
    pending_chunks_fast: bool = False


@dataclass(frozen=True, slots=True)
class DevicePollerStats:
    """Read-only per-device snapshot of adaptive poller state."""

    normal_interval: float
    fast_interval: float | None
    latency_ewma: float | None
    consecutive_failures: int
    observed_batch_capacity: int | None


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

    If ``fast_poll_interval`` is given, devices with a non-empty
    ``NodeState.fast_poll_epcs`` (e.g. instantaneous power) are additionally
    polled on a second, faster cadence (:meth:`schedule_fast_polls`). The fast
    tier shares the same in-flight tracking and latency/backoff signals as
    the normal tier, so a device that turns out to be slow automatically has
    its fast-tier cadence folded back into the normal one instead of being
    hammered independently. See :meth:`_effective_fast_interval`.

    ECHONET Lite does not guarantee that a multi-property GET response
    includes every requested EPC (see spec discussion in the design doc).
    Each scheduled poll (normal or fast tier) therefore has its requested
    EPCs compared against the EPCs actually present in the response frame.
    If fewer EPCs came back than were requested, the device's
    ``observed_batch_capacity`` is shrunk immediately and never increased
    during the runtime. Once a device's capacity is below its target EPC
    count, subsequent polls for that tier are sent as a sequence of chunks,
    one at a time, each only after the previous chunk's response (or timeout)
    is observed (see :meth:`_continue_chunked_poll`). Immediate polls
    (:meth:`schedule_immediate_poll`) are not chunked or tracked this way,
    since they are a one-shot best-effort request.

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
        poll_interval: float,
        awaiting_timeout: float | None = None,
        safety_factor: float = _DEFAULT_SAFETY_FACTOR,
        max_interval: float | None = None,
        fast_poll_interval: float | None = None,
    ) -> None:
        """Initialize the poller with a device manager and polling interval.

        Args:
            device_manager: The device manager to poll.
            poll_interval: Base interval between poll cycles (seconds). Also
                the lower bound of the per-device adaptive interval.
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
                (seconds). If ``None`` (default), the fast tier is disabled
                entirely and ``NodeState.fast_poll_epcs`` is never polled by
                this poller.
        """
        self._device_manager = device_manager
        self._poll_interval = max(1.0, float(poll_interval))
        self._awaiting_timeout = (
            self._poll_interval
            if awaiting_timeout is None
            else max(0.0, float(awaiting_timeout))
        )
        self._safety_factor = max(1.0, float(safety_factor))
        self._max_interval = max(
            self._poll_interval,
            _DEFAULT_MAX_INTERVAL if max_interval is None else float(max_interval),
        )
        self._fast_poll_interval = (
            None if fast_poll_interval is None else max(1.0, float(fast_poll_interval))
        )

        self._pending: set[str] = set()
        self._scheduled: dict[str, asyncio.TimerHandle] = {}
        self._task: asyncio.Task[None] | None = None
        self._fast_task: asyncio.Task[None] | None = None

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
        """Start the periodic polling loop(s)."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._poll_loop(), name="pyhems_property_poller"
            )
        if self._fast_poll_interval is not None and self._fast_task is None:
            self._fast_task = asyncio.get_running_loop().create_task(
                self._fast_poll_loop(), name="pyhems_property_poller_fast"
            )

    def stop(self) -> None:
        """Cancel the polling loop(s) and all scheduled callbacks."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._fast_task is not None:
            self._fast_task.cancel()
            self._fast_task = None
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
        if delay <= 0:
            self._fire_immediate_poll(device_key)
            return

        loop = asyncio.get_running_loop()
        self._scheduled[device_key] = loop.call_later(
            delay, self._scheduled_fire, device_key
        )

    def get_device_stats(self, device_key: str) -> DevicePollerStats:
        """Return a diagnostics-friendly snapshot for one device."""
        state = self._state.get(device_key)
        return DevicePollerStats(
            normal_interval=self._effective_interval(device_key),
            fast_interval=(
                self._effective_fast_interval(device_key)
                if self._fast_poll_interval is not None
                else None
            ),
            latency_ewma=None if state is None else state.latency_ewma,
            consecutive_failures=0 if state is None else state.consecutive_failures,
            observed_batch_capacity=(
                None if state is None else state.observed_batch_capacity
            ),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Run forever, polling on every interval tick."""
        while True:
            await asyncio.sleep(self._poll_interval)
            self._cleanup_stale()
            self.schedule_polls()

    async def _fast_poll_loop(self) -> None:
        """Run forever, polling the fast tier on every (shorter) interval tick."""
        assert self._fast_poll_interval is not None
        while True:
            await asyncio.sleep(self._fast_poll_interval)
            self._cleanup_stale()
            self.schedule_fast_polls()

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
            data = getattr(self._device_manager, "data", {})
            node = data.get(device_key)
            capacity = getattr(node, "observed_batch_capacity", None)
            if isinstance(capacity, int) and capacity >= 1:
                state.observed_batch_capacity = capacity
            self._state[device_key] = state
        return state

    def schedule_polls(self) -> None:
        """Enqueue poll requests for devices that need polling."""
        now = time.monotonic()
        for device_key, node in self._device_manager.data.items():
            if not node.poll_epcs:
                continue
            effective_epcs = self._device_manager.effective_poll_epcs(device_key)
            if not effective_epcs:
                continue
            if device_key in self._pending or device_key in self._scheduled:
                continue
            if self._is_awaiting(device_key):
                continue
            state = self._state.get(device_key)
            last_polled_at = state.last_polled_at if state is not None else None
            if (
                last_polled_at is not None
                and now - last_polled_at < self._effective_interval(device_key)
            ):
                continue
            self._fire_poll(device_key, epcs=effective_epcs)

    def schedule_fast_polls(self) -> None:
        """Enqueue fast-tier poll requests (e.g. instantaneous values).

        No-op if ``fast_poll_interval`` was not configured.
        """
        if self._fast_poll_interval is None:
            return
        now = time.monotonic()
        for device_key, node in self._device_manager.data.items():
            if not node.fast_poll_epcs:
                continue
            effective_epcs = self._device_manager.effective_fast_poll_epcs(device_key)
            if not effective_epcs:
                continue
            if device_key in self._pending or device_key in self._scheduled:
                continue
            if self._is_awaiting(device_key):
                continue
            state = self._state.get(device_key)
            last_polled_at = state.last_fast_polled_at if state is not None else None
            if (
                last_polled_at is not None
                and now - last_polled_at < self._effective_fast_interval(device_key)
            ):
                continue
            self._fire_poll(device_key, epcs=effective_epcs, fast=True)

    def _scheduled_fire(self, device_key: str) -> None:
        self._scheduled.pop(device_key, None)
        self._fire_immediate_poll(device_key)

    def _fire_immediate_poll(self, device_key: str) -> None:
        """Fire an immediate poll using the device's current effective EPC set."""
        effective_epcs = self._device_manager.effective_poll_epcs(device_key)
        if not effective_epcs:
            return
        self._fire_poll(device_key, epcs=effective_epcs, track_requested=False)

    def _fire_poll(
        self,
        device_key: str,
        *,
        epcs: frozenset[int] | None = None,
        fast: bool = False,
        track_requested: bool = True,
    ) -> None:
        if device_key in self._pending:
            return
        self._pending.add(device_key)
        asyncio.get_running_loop().create_task(
            self._poll_node(
                device_key,
                epcs=epcs,
                fast=fast,
                track_requested=track_requested,
            )
        )

    def _is_awaiting(self, device_key: str) -> bool:
        """Return True if a poll response for ``device_key`` is still outstanding.

        If the outstanding poll has been unanswered for longer than
        ``awaiting_timeout``, the wait is abandoned (the entry is cleared)
        and counted as a failure, feeding the exponential backoff in
        :meth:`_effective_interval`.
        """
        state = self._state.get(device_key)
        if state is None or state.awaiting_since is None:
            return False
        if time.monotonic() - state.awaiting_since >= self._awaiting_timeout:
            state.awaiting_since = None
            state.awaiting_tid = None
            state.requested_epcs = None
            state.pending_chunks = []
            state.consecutive_failures += 1
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
        assert self._fast_poll_interval is not None
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

    def _update_batch_capacity(
        self,
        device_key: str,
        state: _DeviceScheduleState,
        requested: frozenset[int],
        received: frozenset[int],
    ) -> None:
        """Shrink ``observed_batch_capacity`` from a partial-response check."""
        if not requested:
            return
        responded = len(requested & received)
        if responded < len(requested):
            previous = state.observed_batch_capacity
            new_capacity = responded if previous is None else min(previous, responded)
            state.observed_batch_capacity = max(1, new_capacity)
            self._device_manager.update_observed_batch_capacity(
                device_key, state.observed_batch_capacity
            )
            _LOGGER.debug(
                "Partial response from %s: requested %d EPCs, got %d; "
                "observed_batch_capacity now %d",
                device_key,
                len(requested),
                responded,
                state.observed_batch_capacity,
            )

    def _continue_chunked_poll(self, device_key: str) -> None:
        """Send the next queued chunk for a poll cycle still in progress."""
        state = self._get_state(device_key)
        if not state.pending_chunks:
            return
        next_chunk = state.pending_chunks.pop(0)
        self._fire_poll(device_key, epcs=next_chunk, fast=state.pending_chunks_fast)

    def _on_frame_received(
        self, device_key: str, tid: int, _esv: int, received_epcs: frozenset[int]
    ) -> None:
        """Clear the awaiting state and update backoff/batch state on any frame.

        Any frame from the device is treated as evidence that it is
        responsive, so the consecutive-failure counter is reset. If the
        frame corresponds to an outstanding poll, the observed latency also
        feeds the EWMA used by :meth:`_effective_interval`, and the EPCs
        actually present in the frame are compared against the EPCs that
        were requested to detect partial responses (see
        :meth:`_update_batch_capacity`). If a chunked poll cycle is still in
        progress for this device, the next chunk is sent immediately.
        """
        state = self._get_state(device_key)
        if state.awaiting_tid is None or tid != state.awaiting_tid:
            return
        sent_at = state.awaiting_since
        state.awaiting_since = None
        state.awaiting_tid = None
        requested = state.requested_epcs
        state.requested_epcs = None
        if sent_at is not None:
            self._update_latency(state, time.monotonic() - sent_at)
        state.consecutive_failures = 0

        if requested is not None:
            self._update_batch_capacity(device_key, state, requested, received_epcs)
            self._continue_chunked_poll(device_key)

    async def _poll_node(
        self,
        device_key: str,
        *,
        epcs: frozenset[int] | None = None,
        fast: bool = False,
        track_requested: bool = True,
    ) -> None:
        send_epcs = epcs
        remaining_chunks: list[frozenset[int]] = []
        if track_requested and epcs is not None:
            capacity = self._get_state(device_key).observed_batch_capacity
            if capacity is not None and len(epcs) > capacity:
                chunks = _chunk_epcs(epcs, capacity)
                send_epcs = chunks[0]
                remaining_chunks = chunks[1:]

        try:
            sent_tid = (
                await self._device_manager.poll_device(device_key)
                if send_epcs is None
                else await self._device_manager.poll_device(device_key, send_epcs)
            )
            if sent_tid is not None:
                now = time.monotonic()
                state = self._get_state(device_key)
                state.awaiting_since = now
                state.awaiting_tid = sent_tid
                state.requested_epcs = send_epcs if track_requested else None
                state.pending_chunks = remaining_chunks if track_requested else []
                state.pending_chunks_fast = fast
                if fast:
                    state.last_fast_polled_at = now
                else:
                    state.last_polled_at = now
            else:
                _LOGGER.debug(
                    "Failed to poll node %s: no poll EPCs or address unknown",
                    device_key,
                )
        except OSError as err:
            _LOGGER.debug(
                "Failed to request properties for node %s: %s", device_key, err
            )
        finally:
            self._pending.discard(device_key)


__all__ = ["DevicePollerStats", "PropertyPoller"]
