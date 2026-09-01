"""Tests for DeviceManager and NodeState."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhems import EOJ, Property
from pyhems.const import (
    ESV_GET_RES,
    ESV_INF,
    ESV_INF_SNA,
    ESV_SET_RES,
    ESV_SET_SNA,
    ESV_SETC,
)
from pyhems.device_manager import (
    DeviceManager,
    NodeState,
    _compute_poll_epcs,
    _decode_ascii_property,
    _parse_property_map,
)
from pyhems.frame import Frame
from pyhems.runtime import (
    HemsFrameEvent,
    HemsInstanceListEvent,
    NotificationRequestResult,
)

# ---------------------------------------------------------------------------
# Private utility functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edt", "expected"),
    [
        (b"", None),
        (b"ABC", "ABC"),
        (b"ABC\x00\x00\x00", "ABC"),
        (b"ABC   ", "ABC"),
        (b"ABC\x00 \x00", "ABC"),
        (b" ABC ", " ABC"),
        (b"\x80\x81", None),
    ],
)
def test_decode_ascii_property(edt: bytes, expected: str | None) -> None:
    """Ensure ASCII properties decode correctly with padding removed."""
    assert _decode_ascii_property(edt) == expected


@pytest.mark.parametrize(
    ("edt", "expected"),
    [
        (b"", frozenset()),
        (bytes.fromhex("03808182"), frozenset({0x80, 0x81, 0x82})),
        (bytes.fromhex("03ff"), frozenset()),
        (bytes.fromhex("10"), frozenset()),
        (
            bytes.fromhex("10FF00000000000000000000000000000000"),
            frozenset({0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xF0}),
        ),
        (
            bytes.fromhex("1001000000000000000000000000000000"),
            frozenset({0x80}),
        ),
        (
            bytes.fromhex("1003000000000000000000000000000000"),
            frozenset({0x80, 0x90}),
        ),
        (bytes.fromhex("05E0"), frozenset()),
        (bytes.fromhex("10FF"), frozenset()),
        (
            bytes.fromhex("160B010109000000010101030303030303"),
            frozenset(
                {
                    0x80,
                    0x81,
                    0x82,
                    0x83,
                    0x87,
                    0x88,
                    0x89,
                    0x8A,
                    0x8B,
                    0x8C,
                    0x8D,
                    0x8E,
                    0x8F,
                    0x90,
                    0x9A,
                    0x9B,
                    0x9C,
                    0x9D,
                    0x9E,
                    0x9F,
                    0xB0,
                    0xB3,
                }
            ),
        ),
    ],
)
def test_parse_property_map(edt: bytes, expected: frozenset[int]) -> None:
    """Ensure property maps parse to expected EPC sets."""
    assert _parse_property_map(edt) == expected


@pytest.mark.parametrize(
    (
        "monitored_epcs",
        "get_epcs",
        "fast_candidate_epcs",
        "confirmed_inf_epcs",
        "expected",
    ),
    [
        pytest.param(
            frozenset({0x80, 0xB0}),
            frozenset({0x80, 0xB0}),
            frozenset(),
            frozenset(),
            (frozenset({0x80, 0xB0}), frozenset()),
            id="nothing_confirmed_polls_everything_gettable",
        ),
        pytest.param(
            frozenset({0x80, 0xB0}),
            frozenset({0x80, 0xB0}),
            frozenset(),
            frozenset({0xB0}),
            (frozenset({0x80}), frozenset()),
            id="confirmed_epc_excluded_from_polling",
        ),
        pytest.param(
            frozenset({0x80, 0xE0}),
            frozenset({0x80, 0xE0}),
            frozenset({0xE0}),
            frozenset(),
            (frozenset({0x80}), frozenset({0xE0})),
            id="fast_candidate_split_into_fast_tier",
        ),
        pytest.param(
            frozenset({0x80}),
            frozenset(),
            frozenset(),
            frozenset(),
            (frozenset(), frozenset()),
            id="not_gettable_epc_is_never_polled",
        ),
    ],
)
def test_compute_poll_epcs(
    monitored_epcs: frozenset[int],
    get_epcs: frozenset[int],
    fast_candidate_epcs: frozenset[int],
    confirmed_inf_epcs: frozenset[int],
    expected: tuple[frozenset[int], frozenset[int]],
) -> None:
    """Poll/fast-poll EPCs are recomputed from confirmed INF subscriptions."""
    assert (
        _compute_poll_epcs(
            monitored_epcs=monitored_epcs,
            get_epcs=get_epcs,
            fast_candidate_epcs=fast_candidate_epcs,
            confirmed_inf_epcs=confirmed_inf_epcs,
        )
        == expected
    )


# ---------------------------------------------------------------------------
# NodeState
# ---------------------------------------------------------------------------


class TestNodeState:
    """Tests for NodeState dataclass."""

    def test_device_key(self) -> None:
        """Device key is node_id-eoj hex."""
        node = NodeState(
            eoj=EOJ(0x013001),
            properties={},
            last_seen=0.0,
            node_id="fe00000000000000000000000000000001",
            manufacturer_code=0x000001,
            manufacturer_name_en=None,
            manufacturer_name_ja=None,
            get_epcs=frozenset(),
            set_epcs=frozenset(),
            inf_epcs=frozenset(),
            poll_epcs=frozenset(),
            fast_poll_epcs=frozenset(),
            product_code=None,
            serial_number=None,
        )
        assert node.device_key == "fe00000000000000000000000000000001-013001"

    def test_manufacturer_name_uses_english_when_known(self) -> None:
        """English name is returned when the manufacturer code is known."""
        node = _make_node()
        node.manufacturer_name_en = "Acme Corp"
        node.manufacturer_name_ja = "アクメ株式会社"
        assert node.manufacturer_name == "Acme Corp"

    def test_manufacturer_name_falls_back_to_hex_code(self) -> None:
        """When the manufacturer code is unknown to the registry."""
        node = _make_node()
        assert node.manufacturer_code == 0x000001
        assert node.manufacturer_name == "0x000001"

    def test_class_name_uses_english_when_known(self) -> None:
        """English class name is returned when the class code is known."""
        node = _make_node()
        node.class_name_en = "Home air conditioner"
        node.class_name_ja = "家庭用エアコン"
        assert node.class_name == "Home air conditioner"

    def test_class_name_falls_back_to_class_code_hex(self) -> None:
        """When the class code is unknown to the registry."""
        node = _make_node(eoj=0x013001)
        assert node.class_name == "ECHONET Lite class 0x0130"

    def test_installation_location_unset(self) -> None:
        """EPC 0x81 absent or set to 0x00 yields None."""
        node = _make_node()
        assert node.installation_location is None
        node.properties[0x81] = b"\x00"
        assert node.installation_location is None

    def test_installation_location_decoded(self) -> None:
        """A known LLLL/NNN byte decodes through to NodeState."""
        node = _make_node(properties={0x81: b"\x29"})  # LLLL=5 (lavatory), NNN=1
        loc = node.installation_location
        assert loc is not None
        assert loc.key == "lavatory"
        assert loc.name == "Lavatory"
        assert loc.instance == 1


# ---------------------------------------------------------------------------
# DeviceManager helpers
# ---------------------------------------------------------------------------


def _make_frame_event(
    node_id: str,
    eoj: EOJ,
    esv: int,
    properties: list[Property],
    received_at: float = 1.0,
) -> HemsFrameEvent:
    frame = Frame(
        seoj=eoj,
        deoj=EOJ(0x05FF01),
        esv=esv,
        properties=properties,
    )
    return HemsFrameEvent(
        received_at=received_at,
        frame=frame,
        node_id=node_id,
        eoj=eoj,
    )


def _make_node(
    eoj: int = 0x013001,
    node_id: str = "fe00000000000000000000000000000001",
    properties: dict[int, bytes] | None = None,
    get_epcs: frozenset[int] | None = None,
    set_epcs: frozenset[int] | None = None,
    inf_epcs: frozenset[int] | None = None,
    poll_epcs: frozenset[int] | None = None,
    fast_poll_epcs: frozenset[int] | None = None,
) -> NodeState:
    return NodeState(
        eoj=EOJ(eoj),
        properties=properties or {},
        last_seen=1.0,
        node_id=node_id,
        manufacturer_code=0x000001,
        manufacturer_name_en=None,
        manufacturer_name_ja=None,
        get_epcs=get_epcs if get_epcs is not None else frozenset(),
        set_epcs=set_epcs if set_epcs is not None else frozenset(),
        inf_epcs=inf_epcs if inf_epcs is not None else frozenset(),
        poll_epcs=poll_epcs if poll_epcs is not None else frozenset({0x80}),
        fast_poll_epcs=fast_poll_epcs if fast_poll_epcs is not None else frozenset(),
        product_code=None,
        serial_number=None,
    )


async def _default_request_notifications(
    _node_id: str, _deoj: EOJ, epcs: list[int]
) -> NotificationRequestResult:
    """Simulate every requested EPC subscribing successfully by default."""
    return NotificationRequestResult(
        successful_epcs=frozenset(epcs),
        failed_epcs=frozenset(),
        unanswered_epcs=frozenset(),
    )


def _make_client() -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=[])
    client.send = AsyncMock(return_value=True)
    client.request_notifications = AsyncMock(side_effect=_default_request_notifications)
    client.get_observed_batch_capacity = MagicMock(return_value=None)
    return client


# ---------------------------------------------------------------------------
# process_frame_event
# ---------------------------------------------------------------------------


class TestProcessFrameEvent:
    """Tests for DeviceManager.process_frame_event."""

    def test_ignores_non_response_frame(self) -> None:
        """Non-response frames (e.g. SETC requests) are ignored."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node()
        dm.data[node.device_key] = node

        event = _make_frame_event(node.node_id, node.eoj, ESV_SETC, [])
        assert dm.process_frame_event(event) is False

    def test_ignores_unknown_device(self) -> None:
        """Frames for unknown devices are ignored."""
        client = _make_client()
        dm = DeviceManager(client, {})

        event = _make_frame_event(
            "fe00000000000000000000000000000001",
            EOJ(0x013001),
            ESV_GET_RES,
            [Property(epc=0x80, edt=b"\x30")],
        )
        assert dm.process_frame_event(event) is False

    def test_updates_properties(self) -> None:
        """Response frames update device properties."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_GET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        assert dm.process_frame_event(event) is True
        assert node.properties[0x80] == b"\x30"

    def test_no_update_when_same_value(self) -> None:
        """No update when property value is unchanged."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x30"})
        dm.data[node.device_key] = node

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_GET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        assert dm.process_frame_event(event) is False

    def test_ignores_set_response(self) -> None:
        """SET_RES frames don't overwrite stored state."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_SET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        assert dm.process_frame_event(event) is False
        assert node.properties[0x80] == b"\x31"

    def test_ignores_set_sna_response(self) -> None:
        """SET_SNA frames don't overwrite stored state."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_SET_SNA, [Property(epc=0x80, edt=b"\x30")]
        )
        assert dm.process_frame_event(event) is False
        assert node.properties[0x80] == b"\x31"

    def test_ignores_inf_sna_response(self) -> None:
        """INF_SNA (0x53) frames don't overwrite stored state.

        A rejected notification subscription typically carries an empty
        EDT, which must not clobber a previously cached property value.
        """
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0xB0: b"\x41"})
        dm.data[node.device_key] = node

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_INF_SNA, [Property(epc=0xB0, edt=b"")]
        )
        assert dm.process_frame_event(event) is False
        assert node.properties[0xB0] == b"\x41"

    def test_inf_frame_updates_properties(self) -> None:
        """INF notification frames update device properties."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_INF, [Property(epc=0x80, edt=b"\x30")]
        )
        assert dm.process_frame_event(event) is True
        assert node.properties[0x80] == b"\x30"

    def test_on_device_updated_callback(self) -> None:
        """Callback is invoked when a device is updated."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        updated_keys: list[str] = []
        dm.on_device_updated(updated_keys.append)

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_GET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        dm.process_frame_event(event)
        assert updated_keys == [node.device_key]

    def test_unsubscribe_callback(self) -> None:
        """Unsubscribe prevents further callbacks."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        updated_keys: list[str] = []
        unsub = dm.on_device_updated(updated_keys.append)
        unsub()

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_GET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        dm.process_frame_event(event)
        assert updated_keys == []

    def test_on_frame_received_fires_even_without_value_change(self) -> None:
        """on_frame_received fires for any response frame, unlike on_device_updated."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x30"})
        dm.data[node.device_key] = node

        received_keys: list[str] = []
        dm.on_frame_received(lambda key, _tid, _esv, _epcs: received_keys.append(key))

        # Same value as already stored: on_device_updated would not fire,
        # but on_frame_received should still fire (a response was observed).
        event = _make_frame_event(
            node.node_id, node.eoj, ESV_GET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        assert dm.process_frame_event(event) is False
        assert received_keys == [node.device_key]

    def test_on_frame_received_passes_epcs_in_frame(self) -> None:
        """on_frame_received passes the set of EPCs present in the frame."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x30"})
        dm.data[node.device_key] = node

        received_epcs: list[frozenset[int]] = []
        dm.on_frame_received(lambda _key, _tid, _esv, epcs: received_epcs.append(epcs))

        event = _make_frame_event(
            node.node_id,
            node.eoj,
            ESV_GET_RES,
            [Property(epc=0x80, edt=b"\x31"), Property(epc=0x81, edt=b"\x01")],
        )
        dm.process_frame_event(event)

        assert received_epcs == [frozenset({0x80, 0x81})]

    def test_on_frame_received_fires_for_set_response(self) -> None:
        """on_frame_received fires even for Set responses."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        received_keys: list[str] = []
        dm.on_frame_received(lambda key, _tid, _esv, _epcs: received_keys.append(key))

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_SET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        dm.process_frame_event(event)
        assert received_keys == [node.device_key]

    def test_on_frame_received_ignores_unknown_device(self) -> None:
        """on_frame_received does not fire for unknown devices."""
        client = _make_client()
        dm = DeviceManager(client, {})

        received_keys: list[str] = []
        dm.on_frame_received(lambda key, _tid, _esv, _epcs: received_keys.append(key))

        event = _make_frame_event(
            "fe00000000000000000000000000000001",
            EOJ(0x013001),
            ESV_GET_RES,
            [Property(epc=0x80, edt=b"\x30")],
        )
        dm.process_frame_event(event)
        assert received_keys == []

    def test_on_frame_received_unsubscribe(self) -> None:
        """Unsubscribe prevents further on_frame_received callbacks."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(properties={0x80: b"\x31"})
        dm.data[node.device_key] = node

        received_keys: list[str] = []
        unsub = dm.on_frame_received(
            lambda key, _tid, _esv, _epcs: received_keys.append(key)
        )
        unsub()

        event = _make_frame_event(
            node.node_id, node.eoj, ESV_GET_RES, [Property(epc=0x80, edt=b"\x30")]
        )
        dm.process_frame_event(event)
        assert received_keys == []


# ---------------------------------------------------------------------------
# process_instance_list_event
# ---------------------------------------------------------------------------


def _make_property_map_edt(epcs: frozenset[int]) -> bytes:
    """Create a property map EDT for list format (count <= 15)."""
    return bytes([len(epcs), *sorted(epcs)])


class TestProcessInstanceListEvent:
    """Tests for DeviceManager.process_instance_list_event."""

    @pytest.mark.asyncio
    async def test_setup_new_device(self) -> None:
        """New device is set up when instance list is received."""
        client = _make_client()
        eoj = EOJ(0x013001)
        node_id = "fe00000000000000000000000000000001"

        get_epcs = frozenset({0x80, 0xB0})
        set_epcs = frozenset({0x80})
        inf_epcs = frozenset({0x80})
        client.get.return_value = [
            Property(epc=0x9D, edt=_make_property_map_edt(inf_epcs)),
            Property(epc=0x9E, edt=_make_property_map_edt(set_epcs)),
            Property(epc=0x9F, edt=_make_property_map_edt(get_epcs)),
            Property(epc=0x8A, edt=b"\x00\x00\x01"),
            Property(epc=0x8C, edt=b"PRODUCT\x00"),
            Property(epc=0x8D, edt=b"SERIAL\x00\x00"),
            Property(epc=0x80, edt=b"\x30"),
            Property(epc=0xB0, edt=b"\x41"),
        ]
        client.get_observed_batch_capacity.return_value = 2

        monitored_epcs = {0x0130: frozenset({0x80, 0xB0})}
        dm = DeviceManager(client, monitored_epcs)

        added_keys: list[str] = []
        dm.on_device_added(added_keys.append)

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[eoj],
            node_id=node_id,
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        assert len(result) == 1
        assert result[0] == f"{node_id}-013001"
        assert added_keys == result

        node = dm.data[result[0]]
        assert node.manufacturer_code == 0x000001
        assert node.product_code == "PRODUCT"
        assert node.serial_number == "SERIAL"
        assert node.observed_batch_capacity == 2
        assert node.get_epcs == get_epcs
        assert node.set_epcs == set_epcs
        assert node.inf_epcs == inf_epcs
        # poll_epcs = (monitored & get) - inf = {0xB0}
        assert node.poll_epcs == frozenset({0xB0})
        assert node.fast_poll_epcs == frozenset()

    @pytest.mark.asyncio
    async def test_setup_new_device_splits_fast_poll_epcs(self) -> None:
        """fast_epcs are split out of poll_epcs into fast_poll_epcs."""
        client = _make_client()
        eoj = EOJ(0x013001)
        node_id = "fe00000000000000000000000000000001"

        get_epcs = frozenset({0x80, 0xB0, 0xE0})
        set_epcs = frozenset({0x80})
        inf_epcs = frozenset({0x80})
        client.get.return_value = [
            Property(epc=0x9D, edt=_make_property_map_edt(inf_epcs)),
            Property(epc=0x9E, edt=_make_property_map_edt(set_epcs)),
            Property(epc=0x9F, edt=_make_property_map_edt(get_epcs)),
            Property(epc=0x8A, edt=b"\x00\x00\x01"),
            Property(epc=0x8C, edt=b"PRODUCT\x00"),
            Property(epc=0x8D, edt=b"SERIAL\x00\x00"),
            Property(epc=0x80, edt=b"\x30"),
            Property(epc=0xB0, edt=b"\x41"),
            Property(epc=0xE0, edt=b"\x00\x64"),
        ]

        monitored_epcs = {0x0130: frozenset({0x80, 0xB0, 0xE0})}
        fast_epcs = {0x0130: frozenset({0xE0})}
        dm = DeviceManager(client, monitored_epcs, fast_epcs=fast_epcs)

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[eoj],
            node_id=node_id,
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        node = dm.data[result[0]]
        # poll_epcs = (monitored & get) - inf - fast = {0xB0}
        assert node.poll_epcs == frozenset({0xB0})
        # fast_poll_epcs = (monitored & get) - inf, intersected with fast_epcs
        assert node.fast_poll_epcs == frozenset({0xE0})

    @pytest.mark.asyncio
    async def test_fast_epcs_not_in_monitored_epcs_are_ignored(self) -> None:
        """fast_epcs entries outside monitored_epcs never appear anywhere."""
        client = _make_client()
        eoj = EOJ(0x013001)
        node_id = "fe00000000000000000000000000000001"

        get_epcs = frozenset({0x80, 0xE0})
        set_epcs = frozenset({0x80})
        inf_epcs = frozenset({0x80})
        client.get.return_value = [
            Property(epc=0x9D, edt=_make_property_map_edt(inf_epcs)),
            Property(epc=0x9E, edt=_make_property_map_edt(set_epcs)),
            Property(epc=0x9F, edt=_make_property_map_edt(get_epcs)),
            Property(epc=0x8A, edt=b"\x00\x00\x01"),
            Property(epc=0x8C, edt=b"PRODUCT\x00"),
            Property(epc=0x8D, edt=b"SERIAL\x00\x00"),
            Property(epc=0x80, edt=b"\x30"),
            Property(epc=0xE0, edt=b"\x00\x64"),
        ]

        # 0xE0 is a fast candidate but is NOT in monitored_epcs, so it should
        # not be polled at all (neither poll_epcs nor fast_poll_epcs).
        monitored_epcs = {0x0130: frozenset({0x80})}
        fast_epcs = {0x0130: frozenset({0xE0})}
        dm = DeviceManager(client, monitored_epcs, fast_epcs=fast_epcs)

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[eoj],
            node_id=node_id,
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        node = dm.data[result[0]]
        assert node.poll_epcs == frozenset()
        assert node.fast_poll_epcs == frozenset()

    @pytest.mark.asyncio
    async def test_inf_req_partial_failure_falls_back_to_polling(self) -> None:
        """EPCs whose INF_REQ (0x63) subscription is rejected stay polled.

        Only the confirmed (0x73) EPC is excluded from ``poll_epcs``; the
        one that came back in a 0x53 (INF_SNA) response remains part of it.
        """
        client = _make_client()
        eoj = EOJ(0x013001)
        node_id = "fe00000000000000000000000000000001"

        get_epcs = frozenset({0x80, 0xB0, 0xB1})
        set_epcs = frozenset({0x80})
        inf_epcs = frozenset({0xB0, 0xB1})
        client.get.return_value = [
            Property(epc=0x9D, edt=_make_property_map_edt(inf_epcs)),
            Property(epc=0x9E, edt=_make_property_map_edt(set_epcs)),
            Property(epc=0x9F, edt=_make_property_map_edt(get_epcs)),
            Property(epc=0x8A, edt=b"\x00\x00\x01"),
            Property(epc=0x8C, edt=b"PRODUCT\x00"),
            Property(epc=0x8D, edt=b"SERIAL\x00\x00"),
            Property(epc=0x80, edt=b"\x30"),
            Property(epc=0xB0, edt=b"\x41"),
            Property(epc=0xB1, edt=b"\x41"),
        ]
        client.request_notifications.side_effect = None
        client.request_notifications.return_value = NotificationRequestResult(
            successful_epcs=frozenset({0xB0}),
            failed_epcs=frozenset({0xB1}),
            unanswered_epcs=frozenset(),
        )

        monitored_epcs = {0x0130: frozenset({0x80, 0xB0, 0xB1})}
        dm = DeviceManager(client, monitored_epcs)

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[eoj],
            node_id=node_id,
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        node = dm.data[result[0]]
        assert node.attempted_inf_epcs == frozenset({0xB0, 0xB1})
        assert node.confirmed_inf_epcs == frozenset({0xB0})
        assert node.failed_inf_epcs == frozenset({0xB1})
        # 0xB0 confirmed -> excluded from polling; 0xB1 rejected -> still polled
        assert node.poll_epcs == frozenset({0x80, 0xB1})

    @pytest.mark.asyncio
    async def test_inf_req_unanswered_epc_falls_back_to_polling(self) -> None:
        """An EPC absent from the 0x63 response (timeout) stays polled."""
        client = _make_client()
        eoj = EOJ(0x013001)
        node_id = "fe00000000000000000000000000000001"

        get_epcs = frozenset({0xB0})
        inf_epcs = frozenset({0xB0})
        client.get.return_value = [
            Property(epc=0x9D, edt=_make_property_map_edt(inf_epcs)),
            Property(epc=0x9E, edt=_make_property_map_edt(frozenset())),
            Property(epc=0x9F, edt=_make_property_map_edt(get_epcs)),
            Property(epc=0x8A, edt=b"\x00\x00\x01"),
            Property(epc=0x8C, edt=b"PRODUCT\x00"),
            Property(epc=0x8D, edt=b"SERIAL\x00\x00"),
            Property(epc=0xB0, edt=b"\x41"),
        ]
        client.request_notifications.side_effect = None
        client.request_notifications.return_value = NotificationRequestResult(
            successful_epcs=frozenset(),
            failed_epcs=frozenset(),
            unanswered_epcs=frozenset({0xB0}),
        )

        monitored_epcs = {0x0130: frozenset({0xB0})}
        dm = DeviceManager(client, monitored_epcs)

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[eoj],
            node_id=node_id,
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        node = dm.data[result[0]]
        assert node.confirmed_inf_epcs == frozenset()
        assert node.failed_inf_epcs == frozenset({0xB0})
        assert node.poll_epcs == frozenset({0xB0})

    @pytest.mark.asyncio
    async def test_inf_req_send_failure_falls_back_to_polling(self) -> None:
        """An OSError sending the INF_REQ leaves the EPC polled."""
        client = _make_client()
        eoj = EOJ(0x013001)
        node_id = "fe00000000000000000000000000000001"

        get_epcs = frozenset({0xB0})
        inf_epcs = frozenset({0xB0})
        client.get.return_value = [
            Property(epc=0x9D, edt=_make_property_map_edt(inf_epcs)),
            Property(epc=0x9E, edt=_make_property_map_edt(frozenset())),
            Property(epc=0x9F, edt=_make_property_map_edt(get_epcs)),
            Property(epc=0x8A, edt=b"\x00\x00\x01"),
            Property(epc=0x8C, edt=b"PRODUCT\x00"),
            Property(epc=0x8D, edt=b"SERIAL\x00\x00"),
            Property(epc=0xB0, edt=b"\x41"),
        ]
        client.request_notifications.side_effect = OSError("network unreachable")

        monitored_epcs = {0x0130: frozenset({0xB0})}
        dm = DeviceManager(client, monitored_epcs)

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[eoj],
            node_id=node_id,
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        node = dm.data[result[0]]
        assert node.confirmed_inf_epcs == frozenset()
        assert node.failed_inf_epcs == frozenset({0xB0})
        assert node.poll_epcs == frozenset({0xB0})

    @pytest.mark.asyncio
    async def test_skips_existing_device(self) -> None:
        """Already known devices are not set up again."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node()
        dm.data[node.device_key] = node

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[node.eoj],
            node_id=node.node_id,
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        assert result == []
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_class_code_filter(self) -> None:
        """Devices with filtered class codes are skipped."""
        client = _make_client()
        dm = DeviceManager(client, {}, class_code_filter=frozenset({0x0130}))

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[EOJ(0x027901)],
            node_id="fe00000000000000000000000000000001",
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        assert result == []
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_manufacturer_code_skips_device(self) -> None:
        """Devices without manufacturer code are skipped."""
        client = _make_client()
        client.get.return_value = [
            Property(epc=0x9D, edt=b"\x00"),
            Property(epc=0x9E, edt=b"\x00"),
            Property(epc=0x9F, edt=b"\x00"),
            Property(epc=0x8A, edt=b""),
            Property(epc=0x8C, edt=b""),
            Property(epc=0x8D, edt=b""),
        ]
        dm = DeviceManager(client, {})

        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[EOJ(0x013001)],
            node_id="fe00000000000000000000000000000001",
            properties={},
        )

        result = await dm.process_instance_list_event(event)
        assert result == []
        assert len(dm.data) == 0

    @pytest.mark.asyncio
    async def test_node_profile_manufacturer_code_does_not_fallback(self) -> None:
        """A missing device manufacturer code prevents device registration."""
        client = _make_client()
        client.get.return_value = [
            Property(epc=0x9D, edt=b"\x00"),
            Property(epc=0x9E, edt=b"\x00"),
            Property(epc=0x9F, edt=b"\x00"),
            Property(epc=0x8A, edt=b""),
        ]
        dm = DeviceManager(client, {})
        eoj = EOJ(0x013001)
        node_id = "fe00000000000000000000000000000001"

        result = await dm.process_instance_list_event(
            HemsInstanceListEvent(
                received_at=1.0,
                instances=[eoj],
                node_id=node_id,
                properties={0x8A: b"\x12\x34\x56"},
            )
        )

        assert result == []
        assert len(dm.data) == 0
        assert 0x83 not in client.get.call_args.args[2]

    @pytest.mark.asyncio
    async def test_node_profile_info_does_not_fallback(self) -> None:
        """Node profile metadata is not used when device values are empty."""
        client = _make_client()
        client.get.return_value = [
            Property(epc=0x9D, edt=b"\x00"),
            Property(epc=0x9E, edt=b"\x00"),
            Property(epc=0x9F, edt=_make_property_map_edt(frozenset({0x80}))),
            Property(epc=0x8A, edt=b"\x00\x00\x01"),
            Property(epc=0x8C, edt=b""),
            Property(epc=0x8D, edt=b""),
        ]
        dm = DeviceManager(client, {})

        node_id = "fe00000000000000000000000000000001"
        event = HemsInstanceListEvent(
            received_at=1.0,
            instances=[EOJ(0x013001)],
            node_id=node_id,
            properties={
                0x8A: b"\x12\x34\x56",
                0x8C: b"NP_PRODUCT\x00",
                0x8D: b"NP_SERIAL\x00",
            },
        )

        result = await dm.process_instance_list_event(event)
        assert len(result) == 1
        node = dm.data[result[0]]
        assert node.manufacturer_code == 0x000001
        assert node.product_code is None
        assert node.serial_number is None


# ---------------------------------------------------------------------------
# poll_device
# ---------------------------------------------------------------------------


class TestPollDevice:
    """Tests for DeviceManager.poll_device."""

    @pytest.mark.asyncio
    async def test_poll_known_device(self) -> None:
        """Polling a known device sends a GET frame."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0}))
        dm.data[node.device_key] = node

        result = await dm.poll_device(node.device_key)
        assert result is not None
        client.send.assert_called_once()
        _node_id, frame = client.send.call_args.args
        assert frame.esv == 0x62
        assert {p.epc for p in frame.properties} == {0x80, 0xB0}
        assert frame.tid == result

    @pytest.mark.asyncio
    async def test_poll_unknown_device(self) -> None:
        """Polling an unknown device returns False."""
        client = _make_client()
        dm = DeviceManager(client, {})

        result = await dm.poll_device("unknown-device")
        assert result is None
        client.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_device_no_poll_epcs(self) -> None:
        """Polling a device with no poll EPCs returns False."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset())
        dm.data[node.device_key] = node

        result = await dm.poll_device(node.device_key)
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_device_send_failure(self) -> None:
        """Polling handles OSError gracefully."""
        client = _make_client()
        client.send.side_effect = OSError("Network error")
        dm = DeviceManager(client, {})
        node = _make_node()
        dm.data[node.device_key] = node

        result = await dm.poll_device(node.device_key)
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_device_with_explicit_epcs(self) -> None:
        """An explicit epcs argument overrides the device's poll_epcs."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0}))
        dm.data[node.device_key] = node

        result = await dm.poll_device(node.device_key, frozenset({0xE0}))
        assert result is not None
        _node_id, frame = client.send.call_args.args
        assert {p.epc for p in frame.properties} == {0xE0}
        assert frame.tid == result

    @pytest.mark.asyncio
    async def test_poll_device_with_empty_explicit_epcs(self) -> None:
        """An explicit empty epcs argument returns False without sending."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0}))
        dm.data[node.device_key] = node

        result = await dm.poll_device(node.device_key, frozenset())
        assert result is None
        client.send.assert_not_called()


# ---------------------------------------------------------------------------
# subscribe_epcs / effective_poll_epcs / effective_fast_poll_epcs
# ---------------------------------------------------------------------------


class TestSubscribeEpcs:
    """Tests for DeviceManager.subscribe_epcs and effective_*_poll_epcs."""

    def test_effective_poll_epcs_unfiltered_before_any_subscription(self) -> None:
        """Before any subscribe_epcs() call, the full candidate set is returned."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0}))
        dm.data[node.device_key] = node

        assert dm.effective_poll_epcs(node.device_key) == frozenset({0x80, 0xB0})

    def test_effective_poll_epcs_unknown_device(self) -> None:
        """An unknown device_key returns an empty set."""
        client = _make_client()
        dm = DeviceManager(client, {})

        assert dm.effective_poll_epcs("unknown-device") == frozenset()

    def test_effective_fast_poll_epcs_unknown_device(self) -> None:
        """An unknown device_key returns an empty set for the fast tier too."""
        client = _make_client()
        dm = DeviceManager(client, {})

        assert dm.effective_fast_poll_epcs("unknown-device") == frozenset()

    def test_effective_poll_epcs_narrowed_by_subscription(self) -> None:
        """After subscribing, only the subscribed EPCs are returned."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0, 0xE0}))
        dm.data[node.device_key] = node

        dm.subscribe_epcs(node.device_key, frozenset({0x80, 0xE0}))

        assert dm.effective_poll_epcs(node.device_key) == frozenset({0x80, 0xE0})

    def test_effective_poll_epcs_excludes_unsubscribed_candidate(self) -> None:
        """An EPC in poll_epcs but never subscribed to is excluded."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0}))
        dm.data[node.device_key] = node

        dm.subscribe_epcs(node.device_key, frozenset({0x80}))

        assert dm.effective_poll_epcs(node.device_key) == frozenset({0x80})

    def test_effective_poll_epcs_empty_after_subscribing_to_nothing(self) -> None:
        """Subscribing with an empty set still confirms the device (no fallback)."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0}))
        dm.data[node.device_key] = node

        dm.subscribe_epcs(node.device_key, frozenset())

        assert dm.effective_poll_epcs(node.device_key) == frozenset()

    def test_unsubscribe_removes_epc_from_effective_set(self) -> None:
        """Unsubscribing removes the EPC once no subscriber remains."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80, 0xB0}))
        dm.data[node.device_key] = node

        unsub = dm.subscribe_epcs(node.device_key, frozenset({0x80, 0xB0}))
        unsub()

        assert dm.effective_poll_epcs(node.device_key) == frozenset()

    def test_unsubscribe_is_idempotent(self) -> None:
        """Calling the unsubscribe function twice has no additional effect."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80}))
        dm.data[node.device_key] = node

        unsub = dm.subscribe_epcs(node.device_key, frozenset({0x80}))
        unsub()
        unsub()

        assert dm.effective_poll_epcs(node.device_key) == frozenset()

    def test_reference_counted_shared_epc(self) -> None:
        """An EPC subscribed by two callers stays subscribed until both unsub."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(poll_epcs=frozenset({0x80}))
        dm.data[node.device_key] = node

        unsub1 = dm.subscribe_epcs(node.device_key, frozenset({0x80}))
        unsub2 = dm.subscribe_epcs(node.device_key, frozenset({0x80}))

        unsub1()
        assert dm.effective_poll_epcs(node.device_key) == frozenset({0x80})

        unsub2()
        assert dm.effective_poll_epcs(node.device_key) == frozenset()

    def test_effective_fast_poll_epcs_narrowed_by_subscription(self) -> None:
        """effective_fast_poll_epcs narrows fast_poll_epcs the same way."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node = _make_node(fast_poll_epcs=frozenset({0xE0, 0xE7}))
        dm.data[node.device_key] = node

        dm.subscribe_epcs(node.device_key, frozenset({0xE7}))

        assert dm.effective_fast_poll_epcs(node.device_key) == frozenset({0xE7})

    def test_subscription_on_one_device_does_not_affect_another(self) -> None:
        """Subscriptions are scoped per device_key."""
        client = _make_client()
        dm = DeviceManager(client, {})
        node1 = _make_node(node_id="node1", poll_epcs=frozenset({0x80}))
        node2 = _make_node(node_id="node2", poll_epcs=frozenset({0x80}))
        dm.data[node1.device_key] = node1
        dm.data[node2.device_key] = node2

        dm.subscribe_epcs(node1.device_key, frozenset({0x80}))

        assert dm.effective_poll_epcs(node1.device_key) == frozenset({0x80})
        # node2 has no confirmed subscription yet: unfiltered fallback.
        assert dm.effective_poll_epcs(node2.device_key) == frozenset({0x80})
        dm.subscribe_epcs(node2.device_key, frozenset())
        assert dm.effective_poll_epcs(node2.device_key) == frozenset()
