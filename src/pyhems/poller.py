"""Property polling scheduler for ECHONET Lite devices."""

from __future__ import annotations

import asyncio
import logging
import time

from .device_manager import DeviceManager

_LOGGER = logging.getLogger(__name__)


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
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        *,
        poll_interval: float,
        awaiting_timeout: float | None = None,
    ) -> None:
        """Initialize the poller with a device manager and polling interval.

        Args:
            device_manager: The device manager to poll.
            poll_interval: Interval between poll cycles (seconds).
            awaiting_timeout: How long to wait for a response to an
                outstanding poll before giving up and allowing a new one to
                be sent (seconds). Defaults to ``poll_interval`` so a device
                that never answers is retried on the next regular cycle.
        """
        self._device_manager = device_manager
        self._poll_interval = max(1.0, float(poll_interval))
        self._awaiting_timeout = (
            self._poll_interval
            if awaiting_timeout is None
            else max(0.0, float(awaiting_timeout))
        )

        self._pending: set[str] = set()
        self._scheduled: dict[str, asyncio.TimerHandle] = {}
        self._task: asyncio.Task[None] | None = None

        # device_key -> monotonic timestamp when a poll was sent and a
        # response is still outstanding.
        self._awaiting: dict[str, float] = {}
        self._unsub_frame_received = device_manager.on_frame_received(
            self._on_frame_received
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the periodic polling loop."""
        if self._task is not None:
            return
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
        self._awaiting.clear()
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

    def _cleanup_stale(self) -> None:
        """Remove pending/scheduled entries for devices no longer present."""
        current = set(self._device_manager.data)
        for device_key in list(self._pending):
            if device_key not in current:
                self._pending.discard(device_key)
        for device_key in list(self._scheduled):
            if device_key not in current:
                self._scheduled.pop(device_key).cancel()
        for device_key in list(self._awaiting):
            if device_key not in current:
                self._awaiting.pop(device_key, None)

    def schedule_polls(self) -> None:
        """Enqueue poll requests for devices that need polling."""
        for device_key, node in self._device_manager.data.items():
            if not node.poll_epcs:
                continue
            if device_key in self._pending or device_key in self._scheduled:
                continue
            if self._is_awaiting(device_key):
                continue
            self._fire_poll(device_key)

    def _scheduled_fire(self, device_key: str) -> None:
        self._scheduled.pop(device_key, None)
        self._fire_poll(device_key)

    def _fire_poll(self, device_key: str) -> None:
        if device_key in self._pending:
            return
        self._pending.add(device_key)
        asyncio.get_running_loop().create_task(self._poll_node(device_key))

    def _is_awaiting(self, device_key: str) -> bool:
        """Return True if a poll response for ``device_key`` is still outstanding.

        If the outstanding poll has been unanswered for longer than
        ``awaiting_timeout``, the wait is abandoned (the entry is cleared)
        so a new poll can be sent.
        """
        sent_at = self._awaiting.get(device_key)
        if sent_at is None:
            return False
        if time.monotonic() - sent_at >= self._awaiting_timeout:
            self._awaiting.pop(device_key, None)
            return False
        return True

    def _on_frame_received(self, device_key: str) -> None:
        """Clear the awaiting state once any frame arrives from the device."""
        self._awaiting.pop(device_key, None)

    async def _poll_node(self, device_key: str) -> None:
        try:
            sent = await self._device_manager.poll_device(device_key)
            if sent:
                self._awaiting[device_key] = time.monotonic()
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
