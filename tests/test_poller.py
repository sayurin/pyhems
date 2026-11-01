"""Tests for PropertyPoller."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhems import EOJ, ESV, GetResponse
from pyhems.device_manager import DeviceManager, NodeState
from pyhems.poller import PropertyPoller, _DeviceScheduleState

_TEST_EOJ = EOJ(0x013001)
_ANSWER = GetResponse(esv=ESV.GET_RES, properties=(), latency=0.5)


def _make_node(
    device_key: str = "node1-013001",
    *,
    poll_epcs: frozenset[int] = frozenset({0xE0}),
    fast_poll_epcs: frozenset[int] = frozenset(),
    eoj: EOJ = _TEST_EOJ,
    manufacturer_code: int = 0x000001,
) -> NodeState:
    """Create a minimal NodeState for testing."""
    return NodeState(
        eoj=eoj,
        properties={},
        node_id=device_key.split("-", 1)[0],
        manufacturer_code=manufacturer_code,
        get_epcs=frozenset(),
        set_epcs=frozenset(),
        inf_epcs=frozenset(),
        poll_epcs=poll_epcs,
        fast_poll_epcs=fast_poll_epcs,
        product_code=None,
        serial_number=None,
    )


def _make_dm(
    *nodes: NodeState,
    capacity: int | None = None,
    responses: Iterable[GetResponse | None] | None = None,
) -> MagicMock:
    """Create a DeviceManager mock whose polls are answered by ``responses``."""
    dm = MagicMock(spec=DeviceManager)
    # Tests refer to devices by their short node IDs ("k1").
    dm.data = {node.node_id: node for node in nodes}
    dm.effective_poll_epcs = MagicMock(side_effect=lambda key: dm.data[key].poll_epcs)
    dm.effective_fast_poll_epcs = MagicMock(
        side_effect=lambda key: dm.data[key].fast_poll_epcs
    )
    dm.get_observed_batch_capacity = MagicMock(return_value=capacity)
    if responses is None:
        dm.poll_device = AsyncMock(return_value=_ANSWER)
    else:
        dm.poll_device = AsyncMock(side_effect=list(responses))
    return dm


def _set_state(
    poller: PropertyPoller, device_key: str, **fields: object
) -> _DeviceScheduleState:
    """Set per-device schedule state fields for testing, creating it if absent."""
    state = poller._state.setdefault(device_key, _DeviceScheduleState())
    for key, value in fields.items():
        setattr(state, key, value)
    return state


def _polled_chunks(dm: MagicMock) -> list[tuple[int, ...]]:
    """Return the EPC chunks passed to poll_device, in call order."""
    return [tuple(call.args[1]) for call in dm.poll_device.await_args_list]


async def _finish_cycles(poller: PropertyPoller) -> None:
    """Wait until every poll cycle in progress has finished."""
    tasks = [
        state.cycle_task
        for state in poller._state.values()
        if state.cycle_task is not None
    ]
    await asyncio.gather(*tasks)


class TestPropertyPollerLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_default_poll_intervals(self) -> None:
        """Omitted intervals use the documented defaults."""
        poller = PropertyPoller(_make_dm())

        assert poller._poll_interval == 60.0
        assert poller._fast_poll_interval == 10.0
        assert poller._awaiting_timeout == 60.0

    @pytest.mark.asyncio
    async def test_start_is_idempotent_and_stop_cancels(self) -> None:
        """start() creates one loop task and stop() cancels it."""
        poller = PropertyPoller(_make_dm(), poll_interval=60)

        poller.start()
        task = poller._task
        poller.start()

        assert poller._task is task
        poller.stop()
        assert poller._task is None
        await asyncio.sleep(0)
        assert task is not None
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_cancels_scheduled_and_cycles(self) -> None:
        """stop() cancels scheduled callbacks and poll cycles in progress."""
        release = asyncio.Event()

        async def poll(*_args: object, **_kwargs: object) -> GetResponse:
            await release.wait()
            return _ANSWER

        dm = _make_dm(_make_node("k1"), _make_node("k2"))
        dm.poll_device = AsyncMock(side_effect=poll)
        poller = PropertyPoller(dm, poll_interval=60)
        poller._fire_poll("k1", fast=False, normal=True)
        poller.schedule_immediate_poll("k2", delay=60)
        cycle = poller._state["k1"].cycle_task

        poller.stop()
        await asyncio.sleep(0)

        assert cycle is not None
        assert cycle.cancelled()
        assert poller._scheduled == {}
        assert poller._state == {}

    @pytest.mark.asyncio
    async def test_restart_after_stop(self) -> None:
        """The poller can be started again after stop()."""
        dm = _make_dm(_make_node("k1"))
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=1)

        poller.start()
        poller.stop()
        poller.start()
        poller._schedule_polls()
        await _finish_cycles(poller)

        dm.poll_device.assert_awaited_once()
        poller.stop()


class TestSchedulePolls:
    """Tests for _schedule_polls."""

    @pytest.mark.asyncio
    async def test_polls_devices_with_poll_epcs(self) -> None:
        """Devices with poll EPCs are polled with the awaiting timeout."""
        dm = _make_dm(_make_node("k1", poll_epcs=frozenset({0xE0})))
        poller = PropertyPoller(dm, poll_interval=60, awaiting_timeout=20)

        poller._schedule_polls()
        await _finish_cycles(poller)

        dm.poll_device.assert_awaited_once_with("k1", (0xE0,), request_timeout=20.0)
        assert poller._state["k1"].last_polled_at is not None

    @pytest.mark.asyncio
    async def test_skips_devices_without_poll_epcs(self) -> None:
        """Devices with no poll EPCs are not polled."""
        dm = _make_dm(_make_node("k1", poll_epcs=frozenset()))
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()

        dm.poll_device.assert_not_awaited()
        assert "k1" not in poller._state

    @pytest.mark.asyncio
    async def test_skips_scheduled_devices(self) -> None:
        """A device with an immediate poll scheduled is skipped."""
        dm = _make_dm(_make_node("k1"))
        poller = PropertyPoller(dm, poll_interval=60)
        poller.schedule_immediate_poll("k1", delay=60)

        poller._schedule_polls()

        dm.poll_device.assert_not_awaited()
        poller.stop()

    @pytest.mark.asyncio
    async def test_skips_device_with_cycle_in_progress(self) -> None:
        """A device is not sent a new cycle while one is in progress."""
        release = asyncio.Event()

        async def poll(*_args: object, **_kwargs: object) -> GetResponse:
            await release.wait()
            return _ANSWER

        dm = _make_dm(_make_node("k1"))
        dm.poll_device = AsyncMock(side_effect=poll)
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await asyncio.sleep(0)
        _set_state(poller, "k1", last_polled_at=None)
        poller._schedule_polls()
        await asyncio.sleep(0)

        assert dm.poll_device.await_count == 1
        release.set()
        await _finish_cycles(poller)

    @pytest.mark.asyncio
    async def test_skips_device_within_adaptive_interval(self) -> None:
        """A device polled recently is not polled again before its interval."""
        dm = _make_dm(_make_node("k1"))
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", last_polled_at=time.monotonic())

        poller._schedule_polls()

        dm.poll_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_polls_once_adaptive_interval_elapses(self) -> None:
        """A device is polled again once its interval has elapsed."""
        dm = _make_dm(_make_node("k1"))
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", last_polled_at=time.monotonic() - 61)

        poller._schedule_polls()
        await _finish_cycles(poller)

        dm.poll_device.assert_awaited_once()


class TestCleanupStale:
    """Tests for _cleanup_stale."""

    @pytest.mark.asyncio
    async def test_removes_state_and_scheduled_for_removed_devices(self) -> None:
        """State, scheduled polls and cycles of removed devices are discarded."""
        release = asyncio.Event()

        async def poll(*_args: object, **_kwargs: object) -> GetResponse:
            await release.wait()
            return _ANSWER

        dm = _make_dm(_make_node("k1"), _make_node("k2"), _make_node("k3"))
        dm.poll_device = AsyncMock(side_effect=poll)
        poller = PropertyPoller(dm, poll_interval=60)
        poller._fire_poll("k1", fast=False, normal=True)
        poller.schedule_immediate_poll("k2", delay=60)
        _set_state(poller, "k3", consecutive_failures=2)
        cycle = poller._state["k1"].cycle_task
        dm.data = {}

        poller._cleanup_stale()
        await asyncio.sleep(0)

        assert poller._state == {}
        assert poller._scheduled == {}
        assert cycle is not None
        assert cycle.cancelled()

    @pytest.mark.asyncio
    async def test_keeps_existing_devices(self) -> None:
        """State of devices still present is kept."""
        dm = _make_dm(_make_node("k1"))
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", consecutive_failures=2)

        poller._cleanup_stale()

        assert poller._state["k1"].consecutive_failures == 2


class TestAdaptiveInterval:
    """Tests for latency- and failure-based interval adaptation."""

    @pytest.mark.asyncio
    async def test_effective_interval_defaults_to_poll_interval(self) -> None:
        """Without observations the interval is the base interval."""
        poller = PropertyPoller(_make_dm(), poll_interval=60)

        assert poller._effective_interval("k1") == 60

    @pytest.mark.asyncio
    async def test_effective_interval_scales_with_latency(self) -> None:
        """A slow device is polled less often."""
        poller = PropertyPoller(_make_dm(), poll_interval=60, safety_factor=2.5)
        _set_state(poller, "k1", latency_ewma=40.0)

        assert poller._effective_interval("k1") == 100.0

    @pytest.mark.asyncio
    async def test_effective_interval_capped_by_max_interval(self) -> None:
        """The interval never exceeds max_interval."""
        poller = PropertyPoller(_make_dm(), poll_interval=60, max_interval=120)
        _set_state(poller, "k1", latency_ewma=1000.0, consecutive_failures=10)

        assert poller._effective_interval("k1") == 120

    @pytest.mark.asyncio
    async def test_effective_interval_backs_off_on_failures(self) -> None:
        """Consecutive failures back off exponentially."""
        poller = PropertyPoller(_make_dm(), poll_interval=60, max_interval=10000)
        _set_state(poller, "k1", consecutive_failures=2)

        assert poller._effective_interval("k1") == 240

    @pytest.mark.asyncio
    async def test_unanswered_poll_counts_failure_and_marks_unavailable(
        self,
    ) -> None:
        """An unanswered scheduled poll is a failure for liveness and backoff."""
        dm = _make_dm(_make_node("k1"), responses=[None])
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert poller._state["k1"].consecutive_failures == 1
        dm.record_poll_failure.assert_called_once_with("k1")
        dm.record_poll_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_answered_poll_updates_latency_and_resets_failures(self) -> None:
        """An answered poll feeds the latency EWMA and clears failures."""
        response = GetResponse(esv=ESV.GET_RES, properties=(), latency=2.0)
        dm = _make_dm(_make_node("k1"), responses=[response])
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", latency_ewma=1.0, consecutive_failures=3)

        poller._fire_poll("k1", fast=False, normal=True)
        await _finish_cycles(poller)

        state = poller._state["k1"]
        assert state.latency_ewma == pytest.approx(0.3 * 2.0 + 0.7 * 1.0)
        assert state.consecutive_failures == 0
        dm.record_poll_success.assert_called_once_with("k1")

    @pytest.mark.asyncio
    async def test_get_sna_response_counts_as_liveness(self) -> None:
        """A 0x52 response shows the device responds."""
        response = GetResponse(esv=ESV.GET_SNA, properties=(), latency=0.1)
        dm = _make_dm(_make_node("k1"), responses=[response])
        poller = PropertyPoller(dm, poll_interval=60)

        poller._fire_poll("k1", fast=False, normal=True)
        await _finish_cycles(poller)

        dm.record_poll_success.assert_called_once_with("k1")
        dm.record_poll_failure.assert_not_called()


class TestFastPollTier:
    """Tests for the high-frequency (fast) polling tier."""

    @pytest.mark.asyncio
    async def test_fast_epcs_are_polled(self) -> None:
        """Devices with fast poll EPCs are polled on the fast tier."""
        dm = _make_dm(
            _make_node("k1", poll_epcs=frozenset(), fast_poll_epcs=frozenset({0xE7}))
        )
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE7,)]
        assert poller._state["k1"].last_fast_polled_at is not None
        assert poller._state["k1"].last_polled_at is None

    @pytest.mark.asyncio
    async def test_skips_within_fast_interval(self) -> None:
        """A device polled on the fast tier recently is skipped."""
        dm = _make_dm(
            _make_node("k1", poll_epcs=frozenset(), fast_poll_epcs=frozenset({0xE7}))
        )
        poller = PropertyPoller(dm, poll_interval=60, fast_poll_interval=10)
        _set_state(poller, "k1", last_fast_polled_at=time.monotonic())

        poller._schedule_polls()

        dm.poll_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_merges_tiers_due_on_same_tick(self) -> None:
        """Both tiers due on the same tick are requested in one cycle."""
        dm = _make_dm(
            _make_node(
                "k1", poll_epcs=frozenset({0xE0}), fast_poll_epcs=frozenset({0xE7})
            )
        )
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE0, 0xE7)]
        state = poller._state["k1"]
        assert state.last_polled_at is not None
        assert state.last_fast_polled_at is not None

    @pytest.mark.asyncio
    async def test_only_fast_tier_when_normal_not_due(self) -> None:
        """Only the fast EPCs are requested when the normal tier is not due."""
        dm = _make_dm(
            _make_node(
                "k1", poll_epcs=frozenset({0xE0}), fast_poll_epcs=frozenset({0xE7})
            )
        )
        poller = PropertyPoller(dm, poll_interval=60)
        _set_state(poller, "k1", last_polled_at=time.monotonic())

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE7,)]

    @pytest.mark.asyncio
    async def test_effective_fast_interval_scales_and_folds(self) -> None:
        """The fast interval grows with latency and folds into the normal one."""
        poller = PropertyPoller(
            _make_dm(), poll_interval=60, fast_poll_interval=10, safety_factor=2.5
        )
        assert poller._effective_fast_interval("k1") == 10
        _set_state(poller, "k1", latency_ewma=8.0)
        assert poller._effective_fast_interval("k1") == 20.0
        _set_state(poller, "k1", latency_ewma=100.0)
        assert poller._effective_fast_interval("k1") == poller._effective_interval("k1")


class TestScheduleImmediatePoll:
    """Tests for schedule_immediate_poll."""

    @pytest.mark.asyncio
    async def test_fires_after_delay_without_liveness(self) -> None:
        """An immediate poll requests both tiers and does not record liveness."""
        dm = _make_dm(
            _make_node(
                "k1", poll_epcs=frozenset({0xE0}), fast_poll_epcs=frozenset({0xE7})
            )
        )
        poller = PropertyPoller(dm, poll_interval=60)

        poller.schedule_immediate_poll("k1", delay=0)
        await asyncio.sleep(0.01)
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE0, 0xE7)]
        dm.record_poll_success.assert_not_called()
        assert "k1" not in poller._scheduled

    @pytest.mark.asyncio
    async def test_ignored_for_unknown_device(self) -> None:
        """An unknown device is not scheduled."""
        poller = PropertyPoller(_make_dm(), poll_interval=60)

        poller.schedule_immediate_poll("unknown", delay=0)

        assert poller._scheduled == {}

    @pytest.mark.asyncio
    async def test_ignored_while_cycle_in_progress(self) -> None:
        """No immediate poll is scheduled while a cycle is in progress."""
        release = asyncio.Event()

        async def poll(*_args: object, **_kwargs: object) -> GetResponse:
            await release.wait()
            return _ANSWER

        dm = _make_dm(_make_node("k1"))
        dm.poll_device = AsyncMock(side_effect=poll)
        poller = PropertyPoller(dm, poll_interval=60)
        poller._fire_poll("k1", fast=False, normal=True)

        poller.schedule_immediate_poll("k1", delay=0)

        assert poller._scheduled == {}
        release.set()
        await _finish_cycles(poller)

    @pytest.mark.asyncio
    async def test_reschedules_existing_immediate_poll(self) -> None:
        """A new immediate poll replaces the one already scheduled."""
        poller = PropertyPoller(_make_dm(_make_node("k1")), poll_interval=60)
        poller.schedule_immediate_poll("k1", delay=60)
        first = poller._scheduled["k1"]

        poller.schedule_immediate_poll("k1", delay=60)

        assert first.cancelled()
        assert poller._scheduled["k1"] is not first
        poller.stop()


class TestChunking:
    """Tests for splitting cycles by the known limit (ADR-0011)."""

    @pytest.mark.asyncio
    async def test_chunks_are_requested_in_order(self) -> None:
        """Every chunk is requested after the previous chunk is answered."""
        dm = _make_dm(
            _make_node("k1", poll_epcs=frozenset({0xE0, 0xE1, 0xE2})), capacity=1
        )
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE0,), (0xE1,), (0xE2,)]
        assert dm.record_poll_success.call_count == 3

    @pytest.mark.asyncio
    async def test_unanswered_chunk_ends_cycle(self) -> None:
        """An unanswered chunk ends the cycle; the rest waits for the next one."""
        dm = _make_dm(
            _make_node("k1", poll_epcs=frozenset({0xE0, 0xE1, 0xE2})),
            capacity=1,
            responses=[_ANSWER, None],
        )
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE0,), (0xE1,)]
        assert poller._state["k1"].consecutive_failures == 1
        dm.record_poll_failure.assert_called_once_with("k1")

    @pytest.mark.asyncio
    async def test_partial_response_is_not_requeued(self) -> None:
        """EPCs missing from a response are left to the next cycle."""
        response = GetResponse(esv=ESV.GET_RES, properties=(), latency=0.1)
        dm = _make_dm(
            _make_node("k1", poll_epcs=frozenset({0xE0, 0xE1, 0xE2})),
            responses=[response],
        )
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE0, 0xE1, 0xE2)]

    @pytest.mark.asyncio
    async def test_policy_batches_respect_incompatible_pairs(self) -> None:
        """Device-specific GET policies split the cycle without learning."""
        dm = _make_dm(
            _make_node(
                "k1",
                eoj=EOJ(0x028701),
                manufacturer_code=0x00000B,
                poll_epcs=frozenset({0xC0, 0xC1, 0xC2, 0xB3, 0xB8, 0xBA}),
                fast_poll_epcs=frozenset({0xB7}),
            ),
            capacity=6,
        )
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xB3, 0xB8, 0xBA, 0xC0, 0xC1, 0xC2), (0xB7,)]


class TestSubscriptionFiltering:
    """Tests that polls use the effective (subscribed) EPC sets."""

    @pytest.mark.asyncio
    async def test_uses_effective_epcs(self) -> None:
        """Only the subscribed EPCs are requested."""
        dm = _make_dm(
            _make_node(
                "k1",
                poll_epcs=frozenset({0xE0, 0xE1}),
                fast_poll_epcs=frozenset({0xE7, 0xE8}),
            )
        )
        dm.effective_poll_epcs = MagicMock(return_value=frozenset({0xE1}))
        dm.effective_fast_poll_epcs = MagicMock(return_value=frozenset({0xE8}))
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()
        await _finish_cycles(poller)

        assert _polled_chunks(dm) == [(0xE1, 0xE8)]

    @pytest.mark.asyncio
    async def test_skips_device_without_effective_epcs(self) -> None:
        """A device with no subscribed EPCs is not polled."""
        dm = _make_dm(_make_node("k1", poll_epcs=frozenset({0xE0})))
        dm.effective_poll_epcs = MagicMock(return_value=frozenset())
        poller = PropertyPoller(dm, poll_interval=60)

        poller._schedule_polls()

        dm.poll_device.assert_not_awaited()
        assert "k1" not in poller._state


class TestDeviceStats:
    """Tests for get_device_stats."""

    @pytest.mark.asyncio
    async def test_reports_intervals_and_signals(self) -> None:
        """Stats expose the current intervals, latency and failures."""
        poller = PropertyPoller(
            _make_dm(), poll_interval=60, fast_poll_interval=10, max_interval=10000
        )
        _set_state(poller, "k1", latency_ewma=1.5, consecutive_failures=1)

        stats = poller.get_device_stats("k1")

        assert stats.normal_interval == 120
        assert stats.fast_interval == 20
        assert stats.latency_ewma == 1.5
        assert stats.consecutive_failures == 1
