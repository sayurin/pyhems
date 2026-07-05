"""Property polling scheduler for ECHONET Lite devices."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

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
            self._fire_poll(device_key)
            return

        loop = asyncio.get_running_loop()
        self._scheduled[device_key] = loop.call_later(
            delay, self._scheduled_fire, device_key
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
        return self._state.setdefault(device_key, _DeviceScheduleState())

    def schedule_polls(self) -> None:
        """Enqueue poll requests for devices that need polling."""
        now = time.monotonic()
        for device_key, node in self._device_manager.data.items():
            if not node.poll_epcs:
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
            self._fire_poll(device_key)

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
            self._fire_poll(device_key, epcs=node.fast_poll_epcs, fast=True)

    def _scheduled_fire(self, device_key: str) -> None:
        self._scheduled.pop(device_key, None)
        self._fire_poll(device_key)

    def _fire_poll(
        self,
        device_key: str,
        *,
        epcs: frozenset[int] | None = None,
        fast: bool = False,
    ) -> None:
        if device_key in self._pending:
            return
        self._pending.add(device_key)
        asyncio.get_running_loop().create_task(
            self._poll_node(device_key, epcs=epcs, fast=fast)
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

    def _on_frame_received(self, device_key: str) -> None:
        """Clear the awaiting state and update backoff state on any frame.

        Any frame from the device is treated as evidence that it is
        responsive, so the consecutive-failure counter is reset. If the
        frame corresponds to an outstanding poll, the observed latency also
        feeds the EWMA used by :meth:`_effective_interval`.
        """
        state = self._get_state(device_key)
        sent_at = state.awaiting_since
        state.awaiting_since = None
        if sent_at is not None:
            self._update_latency(state, time.monotonic() - sent_at)
        state.consecutive_failures = 0

    async def _poll_node(
        self,
        device_key: str,
        *,
        epcs: frozenset[int] | None = None,
        fast: bool = False,
    ) -> None:
        try:
            sent = (
                await self._device_manager.poll_device(device_key)
                if epcs is None
                else await self._device_manager.poll_device(device_key, epcs)
            )
            if sent:
                now = time.monotonic()
                state = self._get_state(device_key)
                state.awaiting_since = now
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


__all__ = ["PropertyPoller"]
