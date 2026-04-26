"""Tests for PropertyPoller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhems import EOJ
from pyhems.device_manager import DeviceManager, NodeState
from pyhems.poller import PropertyPoller


def _make_node(
    device_key: str = "node1-013001",
    *,
    poll_epcs: frozenset[int] = frozenset({0xE0}),
) -> NodeState:
    """Create a minimal NodeState for testing."""
    return NodeState(
        eoj=EOJ(0x013001),
        properties={},
        last_seen=0.0,
        node_id=device_key.split("-", 1)[0],
        manufacturer_code=0x000001,
        manufacturer_name_en=None,
        manufacturer_name_ja=None,
        get_epcs=frozenset(),
        set_epcs=frozenset(),
        inf_epcs=frozenset(),
        poll_epcs=poll_epcs,
        product_code=None,
        serial_number=None,
    )


class TestPropertyPollerLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        """Start creates the background poll-loop task."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        poller.start()
        assert poller._task is not None
        poller.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        """Stop cancels the running poll-loop task."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        poller.start()
        task = poller._task
        poller.stop()
        assert poller._task is None
        assert task is not None
        # Task is in "cancelling" state; let the event loop process it.
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self) -> None:
        """Calling start twice keeps the first task."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        poller.start()
        first_task = poller._task
        poller.start()
        assert poller._task is first_task
        poller.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_scheduled_and_pending(self) -> None:
        """Stop clears both pending and scheduled entries."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        poller = PropertyPoller(dm, poll_interval=60)
        poller.start()
        poller._pending.add("k1")
        handle = asyncio.get_running_loop().call_later(100, lambda: None)
        poller._scheduled["k1"] = handle
        poller.stop()
        assert len(poller._pending) == 0
        assert len(poller._scheduled) == 0


class TestSchedulePolls:
    """Tests for schedule_polls logic."""

    @pytest.mark.asyncio
    async def test_schedule_polls_fires_for_devices_with_poll_epcs(self) -> None:
        """Devices with poll EPCs are enqueued and polled."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", poll_epcs=frozenset({0xE0}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        poller.schedule_polls()

        assert "k1" in poller._pending
        # Let the fire-and-forget task run
        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1")

    @pytest.mark.asyncio
    async def test_schedule_polls_skips_devices_without_poll_epcs(self) -> None:
        """Devices without poll EPCs are skipped."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", poll_epcs=frozenset())}
        dm.poll_device = AsyncMock()
        poller = PropertyPoller(dm, poll_interval=60)

        poller.schedule_polls()

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_polls_skips_pending(self) -> None:
        """Pending devices are not enqueued again."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        poller._pending.add("k1")

        poller.schedule_polls()

        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_polls_skips_scheduled(self) -> None:
        """Scheduled devices are not enqueued again."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock()
        poller = PropertyPoller(dm, poll_interval=60)
        poller._scheduled["k1"] = asyncio.get_running_loop().call_later(
            100, lambda: None
        )

        poller.schedule_polls()

        dm.poll_device.assert_not_called()
        # cleanup
        poller._scheduled["k1"].cancel()


class TestCleanupStale:
    """Tests for _cleanup_stale logic."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_pending_for_removed_devices(self) -> None:
        """Pending entries are removed for missing devices."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        poller._pending.add("gone")

        poller._cleanup_stale()

        assert "gone" not in poller._pending

    @pytest.mark.asyncio
    async def test_cleanup_cancels_scheduled_for_removed_devices(self) -> None:
        """Scheduled handles are canceled for missing devices."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        handle = asyncio.get_running_loop().call_later(100, lambda: None)
        poller._scheduled["gone"] = handle

        poller._cleanup_stale()

        assert "gone" not in poller._scheduled
        assert handle.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_keeps_existing_devices(self) -> None:
        """Existing devices keep their pending and scheduled state."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        poller = PropertyPoller(dm, poll_interval=60)
        poller._pending.add("k1")
        handle = asyncio.get_running_loop().call_later(100, lambda: None)
        poller._scheduled["k1"] = handle

        poller._cleanup_stale()

        assert "k1" in poller._pending
        assert "k1" in poller._scheduled
        handle.cancel()


class TestScheduleImmediatePoll:
    """Tests for schedule_immediate_poll."""

    @pytest.mark.asyncio
    async def test_immediate_poll_fires_after_delay(self) -> None:
        """Zero delay triggers immediate polling."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        poller.schedule_immediate_poll("k1", delay=0)

        assert "k1" in poller._pending
        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1")

    @pytest.mark.asyncio
    async def test_immediate_poll_with_delay_schedules(self) -> None:
        """Positive delay schedules polling for later."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        poller.schedule_immediate_poll("k1", delay=0.05)

        assert "k1" in poller._scheduled
        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

        # Wait for the delay to expire
        await asyncio.sleep(0.1)
        dm.poll_device.assert_called_once_with("k1")
        # cleanup
        poller.stop()

    @pytest.mark.asyncio
    async def test_immediate_poll_ignored_for_unknown_device(self) -> None:
        """Unknown devices are ignored by immediate polling."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)

        poller.schedule_immediate_poll("unknown")

        assert "unknown" not in poller._pending
        assert "unknown" not in poller._scheduled

    @pytest.mark.asyncio
    async def test_immediate_poll_ignored_when_pending(self) -> None:
        """Pending devices are not immediately re-polled."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock()
        poller = PropertyPoller(dm, poll_interval=60)
        poller._pending.add("k1")

        poller.schedule_immediate_poll("k1", delay=0)

        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_immediate_poll_cancels_existing_scheduled(self) -> None:
        """Immediate poll cancels an existing scheduled callback."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        old_handle = asyncio.get_running_loop().call_later(100, lambda: None)
        poller._scheduled["k1"] = old_handle

        poller.schedule_immediate_poll("k1", delay=0)

        assert old_handle.cancelled()
        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1")


class TestPollNode:
    """Tests for _poll_node error handling."""

    @pytest.mark.asyncio
    async def test_poll_node_clears_pending_on_success(self) -> None:
        """Pending flag is cleared when polling succeeds."""
        dm = MagicMock(spec=DeviceManager)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        poller._pending.add("k1")

        await poller._poll_node("k1")

        assert "k1" not in poller._pending

    @pytest.mark.asyncio
    async def test_poll_node_clears_pending_on_failure(self) -> None:
        """Pending flag is cleared when polling returns failure."""
        dm = MagicMock(spec=DeviceManager)
        dm.poll_device = AsyncMock(return_value=False)
        poller = PropertyPoller(dm, poll_interval=60)
        poller._pending.add("k1")

        await poller._poll_node("k1")

        assert "k1" not in poller._pending

    @pytest.mark.asyncio
    async def test_poll_node_clears_pending_on_os_error(self) -> None:
        """Pending flag is cleared when polling raises OSError."""
        dm = MagicMock(spec=DeviceManager)
        dm.poll_device = AsyncMock(side_effect=OSError("network error"))
        poller = PropertyPoller(dm, poll_interval=60)
        poller._pending.add("k1")

        await poller._poll_node("k1")

        assert "k1" not in poller._pending
