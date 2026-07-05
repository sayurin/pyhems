"""Tests for PropertyPoller."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhems import EOJ
from pyhems.device_manager import DeviceManager, NodeState
from pyhems.poller import PropertyPoller, _DeviceScheduleState


def _make_node(
    device_key: str = "node1-013001",
    *,
    poll_epcs: frozenset[int] = frozenset({0xE0}),
    fast_poll_epcs: frozenset[int] = frozenset(),
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
        fast_poll_epcs=fast_poll_epcs,
        product_code=None,
        serial_number=None,
    )


def _set_state(
    poller: PropertyPoller, device_key: str, **fields: object
) -> _DeviceScheduleState:
    """Set per-device schedule state fields for testing, creating it if absent."""
    state = poller._state.setdefault(device_key, _DeviceScheduleState())
    for key, value in fields.items():
        setattr(state, key, value)
    return state


def _awaiting_since(poller: PropertyPoller, device_key: str) -> float | None:
    """Return the device's awaiting_since state, or None if untracked."""
    state = poller._state.get(device_key)
    return None if state is None else state.awaiting_since


def _last_polled_at(poller: PropertyPoller, device_key: str) -> float | None:
    """Return the device's last_polled_at state, or None if untracked."""
    state = poller._state.get(device_key)
    return None if state is None else state.last_polled_at


def _last_fast_polled_at(poller: PropertyPoller, device_key: str) -> float | None:
    """Return the device's last_fast_polled_at state, or None if untracked."""
    state = poller._state.get(device_key)
    return None if state is None else state.last_fast_polled_at


def _latency_ewma(poller: PropertyPoller, device_key: str) -> float | None:
    """Return the device's latency_ewma state, or None if untracked."""
    state = poller._state.get(device_key)
    return None if state is None else state.latency_ewma


def _consecutive_failures(poller: PropertyPoller, device_key: str) -> int:
    """Return the device's consecutive_failures state (0 if untracked)."""
    state = poller._state.get(device_key)
    return 0 if state is None else state.consecutive_failures


def _batch_capacity(poller: PropertyPoller, device_key: str) -> int | None:
    """Return the device's observed_batch_capacity state (None if untracked)."""
    state = poller._state.get(device_key)
    return None if state is None else state.observed_batch_capacity


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

    @pytest.mark.asyncio
    async def test_stop_clears_awaiting_and_unsubscribes(self) -> None:
        """Stop clears awaiting state and unsubscribes from frame events."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        unsub = MagicMock()
        dm.on_frame_received = MagicMock(return_value=unsub)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", awaiting_since=0.0)

        poller.stop()

        assert len(poller._state) == 0
        unsub.assert_called_once()


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
        dm.poll_device.assert_called_once_with("k1", frozenset({0xE0}))

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

    @pytest.mark.asyncio
    async def test_cleanup_removes_awaiting_for_removed_devices(self) -> None:
        """Awaiting entries are removed for devices no longer present."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "gone", awaiting_since=0.0)

        poller._cleanup_stale()

        assert "gone" not in poller._state

    @pytest.mark.asyncio
    async def test_cleanup_keeps_awaiting_for_existing_devices(self) -> None:
        """Awaiting entries are kept for devices that still exist."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", awaiting_since=0.0)

        poller._cleanup_stale()

        assert "k1" in poller._state


class TestAwaitingResponse:
    """Tests for the in-flight / awaiting-response tracking (Step 2)."""

    @pytest.mark.asyncio
    async def test_poll_node_marks_device_awaiting_on_success(self) -> None:
        """A successfully sent poll marks the device as awaiting a response."""
        dm = MagicMock(spec=DeviceManager)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        await poller._poll_node("k1")

        assert _awaiting_since(poller, "k1") is not None

    @pytest.mark.asyncio
    async def test_poll_node_does_not_mark_awaiting_on_failure(self) -> None:
        """A failed send does not mark the device as awaiting a response."""
        dm = MagicMock(spec=DeviceManager)
        dm.poll_device = AsyncMock(return_value=False)
        poller = PropertyPoller(dm, poll_interval=60)

        await poller._poll_node("k1")

        assert _awaiting_since(poller, "k1") is None

    @pytest.mark.asyncio
    async def test_schedule_polls_skips_awaiting_device(self) -> None:
        """A device with an outstanding poll response is not polled again."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", awaiting_since=time.monotonic())

        poller.schedule_polls()

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_polls_retries_after_awaiting_timeout(self) -> None:
        """An expired awaiting entry no longer blocks a new poll."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60, awaiting_timeout=0.01)
        _set_state(poller, "k1", awaiting_since=time.monotonic() - 1.0)

        poller.schedule_polls()

        assert "k1" in poller._pending
        assert _awaiting_since(poller, "k1") is None
        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1", frozenset({0xE0}))

    @pytest.mark.asyncio
    async def test_frame_received_clears_awaiting(self) -> None:
        """Receiving a frame from the device clears its awaiting state."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", awaiting_since=time.monotonic())

        # Simulate DeviceManager invoking the registered callback.
        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", frozenset())

        assert _awaiting_since(poller, "k1") is None

    @pytest.mark.asyncio
    async def test_immediate_poll_skipped_while_awaiting(self) -> None:
        """schedule_immediate_poll does not send an overlapping GET while awaiting."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", awaiting_since=time.monotonic())

        poller.schedule_immediate_poll("k1", delay=0)

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()


class TestAdaptiveInterval:
    """Tests for the per-device adaptive polling interval (Step 3)."""

    @pytest.mark.asyncio
    async def test_effective_interval_defaults_to_poll_interval(self) -> None:
        """With no observations, the effective interval is the base interval."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(dm, poll_interval=60)

        assert poller._effective_interval("k1") == 60.0

    @pytest.mark.asyncio
    async def test_effective_interval_scales_with_latency(self) -> None:
        """A device with high observed latency gets a longer interval."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(dm, poll_interval=60, safety_factor=2.0)
        _set_state(poller, "k1", latency_ewma=40.0)

        assert poller._effective_interval("k1") == 80.0

    @pytest.mark.asyncio
    async def test_effective_interval_capped_by_max_interval(self) -> None:
        """The adaptive interval never exceeds max_interval."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(
            dm, poll_interval=60, safety_factor=10.0, max_interval=120.0
        )
        _set_state(poller, "k1", latency_ewma=1000.0)

        assert poller._effective_interval("k1") == 120.0

    @pytest.mark.asyncio
    async def test_effective_interval_backs_off_on_consecutive_failures(self) -> None:
        """Consecutive unanswered polls back off the interval exponentially."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(dm, poll_interval=60, max_interval=10_000)
        _set_state(poller, "k1", consecutive_failures=3)

        assert poller._effective_interval("k1") == 60.0 * (2.0**3)

    @pytest.mark.asyncio
    async def test_awaiting_timeout_increments_failure_count(self) -> None:
        """An expired awaiting entry counts as a failure for backoff purposes."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(dm, poll_interval=60, awaiting_timeout=0.01)
        _set_state(poller, "k1", awaiting_since=time.monotonic() - 1.0)

        assert poller._is_awaiting("k1") is False
        assert _consecutive_failures(poller, "k1") == 1

    @pytest.mark.asyncio
    async def test_frame_received_updates_latency_and_resets_failures(self) -> None:
        """Receiving a response updates the latency EWMA and resets failures."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(
            poller, "k1", consecutive_failures=5, awaiting_since=time.monotonic() - 5.0
        )

        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", frozenset())

        assert _consecutive_failures(poller, "k1") == 0
        assert _latency_ewma(poller, "k1") == pytest.approx(5.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_frame_received_without_outstanding_poll_only_resets_failures(
        self,
    ) -> None:
        """A frame unrelated to any outstanding poll resets failures but not latency."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", consecutive_failures=2)

        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", frozenset())

        assert _consecutive_failures(poller, "k1") == 0
        assert _latency_ewma(poller, "k1") is None

    @pytest.mark.asyncio
    async def test_schedule_polls_skips_device_within_adaptive_interval(self) -> None:
        """A device is not re-polled before its adaptive interval elapses."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(
            poller,
            "k1",
            latency_ewma=100.0,  # effective interval > 60s
            last_polled_at=time.monotonic(),
        )

        poller.schedule_polls()

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_polls_fires_once_adaptive_interval_elapses(self) -> None:
        """A device is re-polled once its adaptive interval has elapsed."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1")}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60, safety_factor=2.0)
        _set_state(
            poller,
            "k1",
            latency_ewma=40.0,  # effective interval == 80s
            last_polled_at=time.monotonic() - 81.0,
        )

        poller.schedule_polls()

        assert "k1" in poller._pending
        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1", frozenset({0xE0}))

    @pytest.mark.asyncio
    async def test_poll_node_records_last_polled_at_on_success(self) -> None:
        """A successful poll records the last-polled timestamp."""
        dm = MagicMock(spec=DeviceManager)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        await poller._poll_node("k1")

        assert _last_polled_at(poller, "k1") is not None

    @pytest.mark.asyncio
    async def test_cleanup_removes_adaptive_state_for_removed_devices(self) -> None:
        """Latency/failure/last-polled state is removed for missing devices."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(
            poller,
            "gone",
            last_polled_at=time.monotonic(),
            latency_ewma=5.0,
            consecutive_failures=2,
        )

        poller._cleanup_stale()

        assert "gone" not in poller._state


class TestFastPollTier:
    """Tests for the high-frequency (fast) polling tier (Step 4)."""

    @pytest.mark.asyncio
    async def test_fast_tier_disabled_by_default(self) -> None:
        """schedule_fast_polls is a no-op when fast_poll_interval is not set."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", fast_poll_epcs=frozenset({0xE7}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        poller.schedule_fast_polls()

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_fast_tier_does_not_start_loop_when_disabled(self) -> None:
        """No fast-poll task is created when fast_poll_interval is not set."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)

        poller.start()

        assert poller._fast_task is None
        poller.stop()

    @pytest.mark.asyncio
    async def test_fast_tier_starts_loop_when_enabled(self) -> None:
        """A second task is created for the fast tier when configured."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)

        poller.start()

        assert poller._fast_task is not None
        poller.stop()
        assert poller._fast_task is None

    @pytest.mark.asyncio
    async def test_schedule_fast_polls_skips_device_without_fast_epcs(self) -> None:
        """Devices without fast_poll_epcs are not polled by the fast tier."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", fast_poll_epcs=frozenset())}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)

        poller.schedule_fast_polls()

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_fast_polls_fires_with_fast_epcs(self) -> None:
        """A device with fast_poll_epcs is polled using only those EPCs."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", fast_poll_epcs=frozenset({0xE7}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)

        poller.schedule_fast_polls()

        assert "k1" in poller._pending
        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1", frozenset({0xE7}))
        assert _last_fast_polled_at(poller, "k1") is not None

    @pytest.mark.asyncio
    async def test_schedule_fast_polls_skips_within_fast_interval(self) -> None:
        """A device is not re-polled by the fast tier before its interval elapses."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", fast_poll_epcs=frozenset({0xE7}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)
        _set_state(poller, "k1", last_fast_polled_at=time.monotonic())

        poller.schedule_fast_polls()

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_fast_and_normal_tiers_share_awaiting_state(self) -> None:
        """A device awaiting a normal-tier response is skipped by the fast tier."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", fast_poll_epcs=frozenset({0xE7}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)
        _set_state(poller, "k1", awaiting_since=time.monotonic())

        poller.schedule_fast_polls()

        assert "k1" not in poller._pending
        dm.poll_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_effective_fast_interval_defaults_to_fast_base(self) -> None:
        """With no observations, the fast interval is the fast base interval."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)

        assert poller._effective_fast_interval("k1") == 10.0

    @pytest.mark.asyncio
    async def test_effective_fast_interval_scales_with_latency(self) -> None:
        """The fast interval scales with latency like the normal interval."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(
            dm, poll_interval=60, fast_poll_interval=10, safety_factor=2.0
        )
        _set_state(poller, "k1", latency_ewma=6.0)

        assert poller._effective_fast_interval("k1") == 12.0

    @pytest.mark.asyncio
    async def test_effective_fast_interval_folds_into_normal_when_slow(self) -> None:
        """A confirmed-slow device's fast interval is capped to the normal one."""
        dm = MagicMock(spec=DeviceManager)
        poller = PropertyPoller(
            dm,
            poll_interval=60,
            fast_poll_interval=10,
            safety_factor=1.0,
            max_interval=10_000,
        )
        # Latency well beyond both bases: both tiers converge to the same
        # (latency-scaled) value, demonstrating the fold behavior.
        _set_state(poller, "k1", latency_ewma=500.0)

        assert poller._effective_fast_interval("k1") == poller._effective_interval("k1")

    @pytest.mark.asyncio
    async def test_immediate_poll_still_uses_full_poll_epcs(self) -> None:
        """schedule_immediate_poll always uses the device's full poll_epcs."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", fast_poll_epcs=frozenset({0xE7}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)

        poller.schedule_immediate_poll("k1", delay=0)

        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1")
        assert _last_polled_at(poller, "k1") is not None
        assert _last_fast_polled_at(poller, "k1") is None


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


class TestBatchCapacity:
    """Tests for partial-response detection and adaptive batch sizing (Step 5)."""

    @pytest.mark.asyncio
    async def test_partial_response_shrinks_capacity(self) -> None:
        """A response missing requested EPCs shrinks observed_batch_capacity."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        await poller._poll_node("k1", epcs=frozenset({0xE0, 0xE1, 0xE2}))
        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", frozenset({0xE0, 0xE1}))  # 0xE2 missing

        assert _batch_capacity(poller, "k1") == 2

    @pytest.mark.asyncio
    async def test_full_response_does_not_shrink_capacity(self) -> None:
        """A response containing every requested EPC leaves capacity uncapped."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        await poller._poll_node("k1", epcs=frozenset({0xE0, 0xE1}))
        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", frozenset({0xE0, 0xE1}))

        assert _batch_capacity(poller, "k1") is None

    @pytest.mark.asyncio
    async def test_capacity_never_shrinks_below_one(self) -> None:
        """Capacity has a floor of 1 even if a response comes back empty."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)

        await poller._poll_node("k1", epcs=frozenset({0xE0, 0xE1}))
        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", frozenset())

        assert _batch_capacity(poller, "k1") == 1

    @pytest.mark.asyncio
    async def test_capacity_grows_after_consecutive_full_responses(self) -> None:
        """Capacity grows by one after enough consecutive full responses."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", observed_batch_capacity=1)
        callback = dm.on_frame_received.call_args.args[0]

        for _ in range(5):
            await poller._poll_node("k1", epcs=frozenset({0xE0}))
            callback("k1", frozenset({0xE0}))

        assert _batch_capacity(poller, "k1") == 2

    @pytest.mark.asyncio
    async def test_partial_response_without_requested_epcs_is_noop(self) -> None:
        """A frame with no associated requested_epcs does not touch capacity."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.on_frame_received = MagicMock(return_value=unsub)
        poller = PropertyPoller(dm, poll_interval=60)

        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", frozenset({0xE0}))

        assert _batch_capacity(poller, "k1") is None

    @pytest.mark.asyncio
    async def test_schedule_polls_chunks_when_over_capacity(self) -> None:
        """A device whose target EPCs exceed its capacity is sent one chunk."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", poll_epcs=frozenset({0xE0, 0xE1}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", observed_batch_capacity=1)

        poller.schedule_polls()
        await asyncio.sleep(0)

        dm.poll_device.assert_called_once()
        args = dm.poll_device.call_args.args
        assert args[0] == "k1"
        assert len(args[1]) == 1
        assert args[1] <= frozenset({0xE0, 0xE1})

    @pytest.mark.asyncio
    async def test_chunked_poll_sends_next_chunk_on_response(self) -> None:
        """The remaining chunk is sent as soon as the first chunk's response arrives."""
        unsub = MagicMock()
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", poll_epcs=frozenset({0xE0, 0xE1}))}
        dm.on_frame_received = MagicMock(return_value=unsub)
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", observed_batch_capacity=1)

        poller.schedule_polls()
        await asyncio.sleep(0)
        assert dm.poll_device.call_count == 1
        first_chunk = dm.poll_device.call_args.args[1]

        callback = dm.on_frame_received.call_args.args[0]
        callback("k1", first_chunk)
        await asyncio.sleep(0)

        assert dm.poll_device.call_count == 2
        second_chunk = dm.poll_device.call_args.args[1]
        assert first_chunk | second_chunk == frozenset({0xE0, 0xE1})
        assert first_chunk.isdisjoint(second_chunk)

    @pytest.mark.asyncio
    async def test_immediate_poll_is_never_chunked(self) -> None:
        """schedule_immediate_poll bypasses batch-capacity chunking entirely."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {"k1": _make_node("k1", poll_epcs=frozenset({0xE0, 0xE1}))}
        dm.poll_device = AsyncMock(return_value=True)
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", observed_batch_capacity=1)

        poller.schedule_immediate_poll("k1", delay=0)

        await asyncio.sleep(0)
        dm.poll_device.assert_called_once_with("k1")

    @pytest.mark.asyncio
    async def test_cleanup_removes_batch_capacity_for_removed_devices(self) -> None:
        """Batch-capacity state is removed for devices no longer present."""
        dm = MagicMock(spec=DeviceManager)
        dm.data = {}
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "gone", observed_batch_capacity=2)

        poller._cleanup_stale()

        assert "gone" not in poller._state
