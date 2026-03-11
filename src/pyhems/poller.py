"""Property polling scheduler for ECHONET Lite devices."""

from __future__ import annotations

import asyncio
import logging

from .device_manager import DeviceManager

_LOGGER = logging.getLogger(__name__)


class PropertyPoller:
    """Periodically poll devices whose monitored EPCs lack notification support.

    This scheduler iterates ``device_manager.data`` on a fixed interval and
    sends GET requests for each device's ``poll_epcs``.  It also supports
    expedited polling (e.g. after a Set operation) via
    :meth:`schedule_immediate_poll`.
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        *,
        poll_interval: float,
    ) -> None:
        """Initialize the poller with a device manager and polling interval."""
        self._device_manager = device_manager
        self._poll_interval = max(1.0, float(poll_interval))

        self._pending: set[str] = set()
        self._scheduled: dict[str, asyncio.TimerHandle] = {}
        self._task: asyncio.Task[None] | None = None

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

    def schedule_polls(self) -> None:
        """Enqueue poll requests for devices that need polling."""
        for device_key, node in self._device_manager.data.items():
            if not node.poll_epcs:
                continue
            if device_key in self._pending or device_key in self._scheduled:
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

    async def _poll_node(self, device_key: str) -> None:
        try:
            sent = await self._device_manager.poll_device(device_key)
            if not sent:
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
