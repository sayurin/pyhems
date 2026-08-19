"""Tests for runtime client."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyhems import (
    CONTROLLER_INSTANCE,
    ECHONET_MULTICAST,
    EOJ,
    EPC_IDENTIFICATION_NUMBER,
    EPC_MANUFACTURER_CODE,
    EPC_PRODUCT_CODE,
    EPC_SELF_NODE_INSTANCE_LIST,
    EPC_SERIAL_NUMBER,
    ESV_GET,
    ESV_INF,
    ESV_INF_SNA,
    ESV_INFC,
    ESV_INFC_RES,
    NODE_PROFILE_INSTANCE,
    Frame,
    Property,
)
from pyhems.runtime import (
    HemsClient,
    HemsErrorEvent,
    HemsFrameEvent,
    HemsInstanceListEvent,
    RuntimeEvent,
)


class TestRuntimeEvents:
    """Tests for runtime event classes."""

    def test_runtime_event_has_received_at(self) -> None:
        """Test RuntimeEvent has received_at timestamp."""
        event = RuntimeEvent(received_at=1.0)
        assert event.received_at == 1.0

    def test_frame_event_fields(self) -> None:
        """Test HemsFrameEvent field values."""
        frame = Frame(tid=1, seoj=EOJ(0x010101), deoj=EOJ(0x020202), esv=0x72)
        event = HemsFrameEvent(
            received_at=1.0,
            frame=frame,
            node_id="abc123",
            eoj=EOJ(0x010101),
        )
        assert event.received_at == 1.0
        assert event.frame == frame
        assert event.node_id == "abc123"
        assert event.eoj == EOJ(0x010101)

    def test_instance_list_event_fields(self) -> None:
        """Test HemsInstanceListEvent field values."""
        event = HemsInstanceListEvent(
            received_at=2.0,
            instances=[EOJ(0x010101), EOJ(0x013001)],
            node_id="def456",
            properties={0x83: b"\xfe" + b"\x00" * 16, 0x8C: b"PRODUCT"},
        )
        assert event.received_at == 2.0
        assert event.instances == [EOJ(0x010101), EOJ(0x013001)]
        assert event.node_id == "def456"
        assert event.properties == {0x83: b"\xfe" + b"\x00" * 16, 0x8C: b"PRODUCT"}

    def test_error_event_fields(self) -> None:
        """Test HemsErrorEvent field values."""
        error = RuntimeError("test error")
        event = HemsErrorEvent(received_at=3.0, error=error)
        assert event.received_at == 3.0
        assert event.error == error


class TestHemsClient:
    """Tests for HemsClient class."""

    def test_client_init(self) -> None:
        """Test client initialization."""
        client = HemsClient(interface="192.168.1.1", poll_interval=30.0)
        assert client._interface == "192.168.1.1"
        assert client._poll_interval == 30.0

    def test_subscribe_unsubscribe(self) -> None:
        """Test subscribing and unsubscribing callbacks."""
        client = HemsClient()
        events: list[RuntimeEvent] = []

        def callback(event: RuntimeEvent) -> None:
            events.append(event)

        unsubscribe = client.subscribe(callback)
        assert callback in client._callbacks

        # Dispatch an event
        event = RuntimeEvent(received_at=1.0)
        client._dispatch(event)
        assert len(events) == 1

        # Unsubscribe
        unsubscribe()
        assert callback not in client._callbacks

        # Dispatch another event - should not be received
        client._dispatch(RuntimeEvent(received_at=2.0))
        assert len(events) == 1

    def test_device_address_mapping(self) -> None:
        """Test device address mapping."""
        client = HemsClient()
        identification = "fe00000000000000000000000000000001"

        client._device_addresses.forceput("192.168.1.100", identification)
        assert client._device_addresses.get("192.168.1.100") == identification
        assert client._device_addresses.inverse.get(identification) == "192.168.1.100"

        # Unknown device returns None
        unknown = "fe00000000000000000000000000000002"
        assert client._device_addresses.inverse.get(unknown) is None

    @pytest.mark.asyncio
    async def test_async_set_property_sends_setc_frame(self) -> None:
        """Test set_property builds and sends a SetC frame."""
        original_tid = Frame._tid_counter
        client = HemsClient()
        mock_protocol = MagicMock()
        client._protocol = mock_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.100", node_id)

        sent = await client.set_property(
            node_id=node_id,
            deoj=EOJ(0x013001),
            epc=0x80,
            edt=b"\x30",
        )

        assert sent is True
        mock_protocol.send.assert_called_once()
        payload, address = mock_protocol.send.call_args.args
        assert address == "192.168.1.100"
        frame = Frame.decode(payload)
        assert frame.esv == 0x61
        assert frame.deoj == EOJ(0x013001)
        assert frame.properties == [Property(epc=0x80, edt=b"\x30")]
        Frame._tid_counter = original_tid

    @pytest.mark.asyncio
    async def test_async_set_properties_without_properties_returns_false(self) -> None:
        """Test set_properties returns False when nothing is requested."""
        client = HemsClient()
        result = await client.set_properties(
            node_id="fe00000000000000000000000000000001",
            deoj=EOJ(0x013001),
            properties=[],
        )
        assert result is False


class TestNodeProbe:
    """Tests for node probe functionality."""

    @staticmethod
    def _simulate_receive(client: HemsClient, frame: Frame, address: str) -> None:
        """Simulate receiving a frame via _on_receive."""
        frame_data = frame.encode()
        client._on_receive(frame_data, (address, 3610))

    def test_next_tid(self) -> None:
        """Test transaction ID generation."""
        original_tid = Frame._tid_counter

        try:
            Frame._tid_counter = 0
            tid1 = Frame.next_tid()
            tid2 = Frame.next_tid()
            assert tid1 == 1
            assert tid2 == 2

            # Test wrap around - ensure zero is not returned (we skip 0)
            Frame._tid_counter = 0xFFFF
            tid3 = Frame.next_tid()
            assert tid3 != 0
        finally:
            Frame._tid_counter = original_tid

    @pytest.mark.asyncio
    async def test_async_probe_nodes_not_running(self) -> None:
        """Test probe fails when not running."""
        client = HemsClient()
        result = await client.probe_nodes()
        assert result is False

    @pytest.mark.asyncio
    async def test_async_probe_nodes_sends_correct_frame(self) -> None:
        """Test probe sends correct Get request with default EPCs."""
        client = HemsClient()

        # Mock protocol
        mock_protocol = MagicMock()
        client._protocol = mock_protocol

        result = await client.probe_nodes()
        assert result is True

        # Verify send was called
        mock_protocol.send.assert_called_once()
        call_args = mock_protocol.send.call_args
        data, address = call_args[0]

        # Verify address is multicast address (not a tuple)
        assert address == ECHONET_MULTICAST

        # Decode the frame to verify
        frame = Frame.decode(data)
        assert frame.seoj == CONTROLLER_INSTANCE
        assert frame.deoj == NODE_PROFILE_INSTANCE
        assert frame.esv == ESV_GET
        # Default probe only includes required EPCs (identification and instance list)
        assert len(frame.properties) == 2

        epcs = [p.epc for p in frame.properties]
        assert EPC_IDENTIFICATION_NUMBER in epcs
        assert EPC_SELF_NODE_INSTANCE_LIST in epcs

    @pytest.mark.asyncio
    async def test_async_probe_nodes_with_extra_epcs(self) -> None:
        """Test probe sends extra EPCs when specified in constructor."""
        extra_epcs = [EPC_MANUFACTURER_CODE, EPC_PRODUCT_CODE, EPC_SERIAL_NUMBER]
        client = HemsClient(extra_epcs=extra_epcs)

        # Mock protocol
        mock_protocol = MagicMock()
        client._protocol = mock_protocol

        result = await client.probe_nodes()
        assert result is True

        # Verify send was called
        mock_protocol.send.assert_called_once()
        call_args = mock_protocol.send.call_args
        data, _address = call_args[0]

        # Decode the frame to verify
        frame = Frame.decode(data)
        assert len(frame.properties) == 5

        epcs = [p.epc for p in frame.properties]
        assert EPC_IDENTIFICATION_NUMBER in epcs
        assert EPC_SELF_NODE_INSTANCE_LIST in epcs
        assert EPC_MANUFACTURER_CODE in epcs
        assert EPC_PRODUCT_CODE in epcs
        assert EPC_SERIAL_NUMBER in epcs

    @pytest.mark.asyncio
    async def test_start_creates_poll_task(self) -> None:
        """Test that start creates a polling task."""
        with patch(
            "pyhems.runtime.create_multicast_socket", new_callable=AsyncMock
        ) as mock_create:
            mock_protocol = MagicMock()
            mock_create.return_value = mock_protocol

            client = HemsClient(poll_interval=60.0)
            await client.start()

            assert client._protocol is not None
            assert client._poll_task is not None

            # Clean up
            await client.stop()
            assert client._protocol is None
            assert client._poll_task is None
            # Ensure protocol is closed (protocol manages transport internally)
            mock_protocol.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_poll_loop_waits_before_initial_probe(self) -> None:
        """Test that the first periodic probe is delayed."""
        client = HemsClient(poll_interval=60.0)
        client._protocol = MagicMock()

        with (
            patch(
                "pyhems.runtime.asyncio.sleep",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ) as mock_sleep,
            patch.object(client, "probe_nodes", new_callable=AsyncMock) as mock_probe,
        ):
            await client._poll_loop()

        mock_sleep.assert_awaited_once_with(60.0)
        mock_probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_frame_dispatches_instance_list_event(self) -> None:
        """Test that Get_Res with instance list dispatches event."""
        client = HemsClient()
        events: list[RuntimeEvent] = []
        client.subscribe(events.append)

        # Create a Get_Res frame from node profile
        # with identification number and instance list
        identification = b"\xfe" + b"\x00" * 16
        identification_hex = identification.hex()
        instance_list = bytes([2, 0x01, 0x30, 0x01, 0x02, 0x87, 0x01])

        frame = Frame(
            tid=1,
            seoj=NODE_PROFILE_INSTANCE,
            deoj=CONTROLLER_INSTANCE,
            esv=0x72,  # Get_Res
            properties=[
                Property(epc=EPC_IDENTIFICATION_NUMBER, edt=identification),
                Property(epc=EPC_SELF_NODE_INSTANCE_LIST, edt=instance_list),
            ],
        )

        self._simulate_receive(client, frame, "192.168.1.100")

        # Should dispatch HemsInstanceListEvent
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, HemsInstanceListEvent)
        assert event.node_id == identification_hex
        assert event.instances == [EOJ(0x013001), EOJ(0x028701)]

        # Address should be registered
        assert (
            client._device_addresses.inverse.get(identification_hex) == "192.168.1.100"
        )

    @pytest.mark.asyncio
    async def test_process_frame_discards_request_esv(self) -> None:
        """Test that request ESVs (0x60-0x6F) are discarded."""
        client = HemsClient()
        events: list[RuntimeEvent] = []
        client.subscribe(events.append)

        # Create a Get request frame (ESV=0x62, should be discarded)
        frame = Frame(
            tid=1,
            seoj=CONTROLLER_INSTANCE,
            deoj=NODE_PROFILE_INSTANCE,
            esv=0x62,  # Get (request ESV)
            properties=[Property(epc=EPC_IDENTIFICATION_NUMBER)],
        )

        self._simulate_receive(client, frame, "192.168.1.100")

        # No events should be dispatched
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_process_frame_requires_identification_for_non_node_profile(
        self,
    ) -> None:
        """Test that non-node-profile frames require known identification."""
        client = HemsClient()
        events: list[RuntimeEvent] = []
        client.subscribe(events.append)

        # Create a Get_Res frame from a device (not node profile)
        frame = Frame(
            tid=1,
            seoj=EOJ(0x013001),  # Air conditioner
            deoj=CONTROLLER_INSTANCE,
            esv=0x72,  # Get_Res
            properties=[Property(epc=0x80, edt=b"\x30")],  # Power status
        )

        # Device not registered - should not dispatch
        self._simulate_receive(client, frame, "192.168.1.100")
        assert len(events) == 0

        # Register device and try again
        identification = (b"\xfe" + b"\x00" * 16).hex()
        client._device_addresses.forceput("192.168.1.100", identification)

        self._simulate_receive(client, frame, "192.168.1.100")

        # Now should dispatch
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, HemsFrameEvent)
        assert event.node_id == identification

    @pytest.mark.asyncio
    async def test_bidict_reverse_lookup(self) -> None:
        """Test bidict allows reverse lookup by identification."""
        client = HemsClient()
        identification_bytes = b"\xfe" + b"\x00" * 16
        identification = identification_bytes.hex()

        client._device_addresses.forceput("192.168.1.100", identification)

        # Forward lookup: address -> identification
        assert client._device_addresses.get("192.168.1.100") == identification

        # Reverse lookup: identification -> address
        assert client._device_addresses.inverse.get(identification) == "192.168.1.100"

    @pytest.mark.asyncio
    async def test_bidict_forceput_handles_address_change(self) -> None:
        """Test that address changes are handled correctly with forceput."""
        client = HemsClient()
        events: list[RuntimeEvent] = []
        client.subscribe(events.append)

        identification_bytes = b"\xfe" + b"\x00" * 16
        identification_hex = identification_bytes.hex()
        instance_list = bytes([1, 0x01, 0x30, 0x01])

        # First probe response from 192.168.1.100
        frame1 = Frame(
            tid=1,
            seoj=NODE_PROFILE_INSTANCE,
            deoj=CONTROLLER_INSTANCE,
            esv=0x72,
            properties=[
                Property(epc=EPC_IDENTIFICATION_NUMBER, edt=identification_bytes),
                Property(epc=EPC_SELF_NODE_INSTANCE_LIST, edt=instance_list),
            ],
        )
        self._simulate_receive(client, frame1, "192.168.1.100")
        assert (
            client._device_addresses.inverse.get(identification_hex) == "192.168.1.100"
        )

        # Device gets new IP address (192.168.1.200)
        frame2 = Frame(
            tid=2,
            seoj=NODE_PROFILE_INSTANCE,
            deoj=CONTROLLER_INSTANCE,
            esv=0x72,
            properties=[
                Property(epc=EPC_IDENTIFICATION_NUMBER, edt=identification_bytes),
                Property(epc=EPC_SELF_NODE_INSTANCE_LIST, edt=instance_list),
            ],
        )
        self._simulate_receive(client, frame2, "192.168.1.200")

        # Address should be updated
        assert (
            client._device_addresses.inverse.get(identification_hex) == "192.168.1.200"
        )

        # Direct lookup should also work
        assert client._device_addresses.get("192.168.1.200") == identification_hex
        # Old address should no longer be mapped
        assert client._device_addresses.get("192.168.1.100") is None


class TestAsyncGet:
    """Tests for get method with retry support."""

    @staticmethod
    def _simulate_receive(client: HemsClient, frame: Frame, address: str) -> None:
        """Simulate receiving a frame via _on_receive."""
        frame_data = frame.encode()
        client._on_receive(frame_data, (address, 3610))

    @pytest.fixture
    def client_with_protocol(self) -> HemsClient:
        """Create a client with mocked protocol."""
        client = HemsClient()
        client._protocol = MagicMock()
        client._protocol.send = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_async_get_success(self, client_with_protocol: HemsClient) -> None:
        """Test get with successful 0x72 response."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        # Start the get request
        get_task = asyncio.create_task(
            client.get(node_id, EOJ(0x013001), [0x80, 0xB0], request_timeout=1.0)
        )

        # Give time for request to be registered
        await asyncio.sleep(0.01)

        # Simulate receiving response
        tid = next(iter(client._pending_gets.keys()))
        response_frame = Frame(
            tid=tid,
            seoj=EOJ(0x013001),
            deoj=EOJ(0x05FF01),
            esv=0x72,
            properties=[
                Property(epc=0x80, edt=b"\x30"),
                Property(epc=0xB0, edt=b"\x42"),
            ],
        )
        self._simulate_receive(client, response_frame, "192.168.1.10")

        result = await get_task
        assert len(result) == 2
        assert result[0].epc == 0x80
        assert result[0].edt == b"\x30"
        assert result[1].epc == 0xB0
        assert result[1].edt == b"\x42"

    @pytest.mark.asyncio
    async def test_async_get_partial_missing_epc_retry(
        self, client_with_protocol: HemsClient
    ) -> None:
        """Test get retries when some EPCs are missing from response."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        async def run_test() -> list[Property]:
            return [
                Property(epc=property.epc, edt=property.edt)
                for property in await client.get(
                    node_id, EOJ(0x013001), [0x80, 0xB0, 0xBB], request_timeout=1.0
                )
            ]

        get_task = asyncio.create_task(run_test())

        # First response: partial (0x52), only 0x80 present
        # 0xB0 and 0xBB are completely missing from the response
        await asyncio.sleep(0.01)
        if client._pending_gets:
            tid1 = next(iter(client._pending_gets.keys()))
            response1 = Frame(
                tid=tid1,
                seoj=EOJ(0x013001),
                deoj=EOJ(0x05FF01),
                esv=0x52,  # Partial response
                properties=[
                    Property(epc=0x80, edt=b"\x30"),  # Success
                ],
            )
            self._simulate_receive(client, response1, "192.168.1.10")

        # Second response: retry succeeds for remaining
        await asyncio.sleep(0.01)
        if client._pending_gets:
            tid2 = next(iter(client._pending_gets.keys()))
            response2 = Frame(
                tid=tid2,
                seoj=EOJ(0x013001),
                deoj=EOJ(0x05FF01),
                esv=0x72,  # Full success
                properties=[
                    Property(epc=0xB0, edt=b"\x45"),  # FAN mode
                    Property(epc=0xBB, edt=b"\x1a"),  # Temperature
                ],
            )
            self._simulate_receive(client, response2, "192.168.1.10")

        result = await get_task
        assert len(result) == 3
        # Results in original request order
        assert result[0].epc == 0x80
        assert result[0].edt == b"\x30"
        assert result[1].epc == 0xB0
        assert result[1].edt == b"\x45"
        assert result[2].epc == 0xBB
        assert result[2].edt == b"\x1a"

    @pytest.mark.asyncio
    async def test_async_get_sna_no_retry(
        self, client_with_protocol: HemsClient
    ) -> None:
        """Test get does NOT retry for SNA (empty value) properties."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        async def run_test() -> list[Property]:
            return [
                Property(epc=property.epc, edt=property.edt)
                for property in await client.get(
                    node_id, EOJ(0x013001), [0x80, 0xB0], request_timeout=1.0
                )
            ]

        get_task = asyncio.create_task(run_test())

        # Response: 0x80 success, 0xB0 SNA (empty)
        await asyncio.sleep(0.01)
        if client._pending_gets:
            tid1 = next(iter(client._pending_gets.keys()))
            response1 = Frame(
                tid=tid1,
                seoj=EOJ(0x013001),
                deoj=EOJ(0x05FF01),
                esv=0x52,  # Partial response
                properties=[
                    Property(epc=0x80, edt=b"\x30"),  # Success
                    Property(epc=0xB0, edt=b""),  # SNA (empty)
                ],
            )
            self._simulate_receive(client, response1, "192.168.1.10")

        # Should finish immediately without retry
        result = await get_task
        assert len(result) == 2
        assert result[0].epc == 0x80
        assert result[0].edt == b"\x30"
        assert result[1].epc == 0xB0
        assert result[1].edt == b""  # Empty value returned

    @pytest.mark.asyncio
    async def test_async_get_timeout(self, client_with_protocol: HemsClient) -> None:
        """Test get times out and returns empty properties."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        result = await client.get(
            node_id, EOJ(0x013001), [0x80, 0xB0], request_timeout=0.1, max_retries=0
        )

        # Should return empty properties for failed EPCs
        assert len(result) == 2
        assert result[0].epc == 0x80
        assert result[0].edt == b""
        assert result[1].epc == 0xB0
        assert result[1].edt == b""

    @pytest.mark.asyncio
    async def test_async_get_empty_epcs(self, client_with_protocol: HemsClient) -> None:
        """Test get with empty EPC list returns empty."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)
        result = await client.get(node_id, EOJ(0x013001), [])
        assert result == []

    @pytest.mark.asyncio
    async def test_async_get_no_protocol(self) -> None:
        """Test get without protocol returns empty."""
        client = HemsClient()
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)
        # client._protocol is None
        result = await client.get(node_id, EOJ(0x013001), [0x80])
        assert result == []


class TestInfcHandling:
    """Tests for INFC (0x74) confirmation handling."""

    @staticmethod
    def _simulate_receive(client: HemsClient, frame: Frame, address: str) -> None:
        """Simulate receiving a frame via _on_receive."""
        frame_data = frame.encode()
        client._on_receive(frame_data, (address, 3610))

    @pytest.fixture
    def client_with_protocol(self) -> HemsClient:
        """Create a client with mocked protocol."""
        client = HemsClient()
        client._protocol = MagicMock()
        client._protocol.send = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_infc_response_sent_on_receipt(
        self, client_with_protocol: HemsClient
    ) -> None:
        """Test that INFC_RES (0x7A) is sent when INFC (0x74) is received."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        # Create an INFC (0x74) frame from a device
        infc_frame = Frame(
            tid=42,
            seoj=EOJ(0x013001),
            deoj=CONTROLLER_INSTANCE,
            esv=ESV_INFC,
            properties=[
                Property(epc=0x80, edt=b"\x30"),
                Property(epc=0xB0, edt=b"\x42"),
            ],
        )

        # Simulate receiving the INFC frame
        self._simulate_receive(client, infc_frame, "192.168.1.10")

        # Give async task time to complete
        await asyncio.sleep(0.1)

        # Verify that protocol.send was called for the INFC_RES response
        assert client._protocol is not None
        send: Any = client._protocol.send
        assert send.call_count >= 1

        # Get the response frame data from the send call
        send_calls = list(send.call_args_list)
        assert len(send_calls) > 0

        # Decode the last sent frame to verify it's INFC_RES
        last_call_data = send_calls[-1][0][0]  # Get the frame data from the call
        response_frame = Frame.decode(last_call_data)

        # Verify the response frame is INFC_RES (0x7A)
        assert response_frame.esv == ESV_INFC_RES
        # Verify transaction ID is preserved
        assert response_frame.tid == 42
        # Verify seoj/deoj are swapped
        assert response_frame.seoj == CONTROLLER_INSTANCE
        assert response_frame.deoj == EOJ(0x013001)
        # Verify properties are included
        assert len(response_frame.properties) == 2


class TestRequestNotifications:
    """Tests for HemsClient.request_notifications (0x63 -> 0x73/0x53)."""

    @staticmethod
    def _simulate_receive(client: HemsClient, frame: Frame, address: str) -> None:
        """Simulate receiving a frame via _on_receive."""
        frame_data = frame.encode()
        client._on_receive(frame_data, (address, 3610))

    @pytest.fixture
    def client_with_protocol(self) -> HemsClient:
        """Create a client with mocked protocol."""
        client = HemsClient()
        client._protocol = MagicMock()
        client._protocol.send = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_all_epcs_confirmed_via_inf(
        self, client_with_protocol: HemsClient
    ) -> None:
        """A 0x73 response listing all requested EPCs confirms all of them."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        request_task = asyncio.create_task(
            client.request_notifications(
                node_id, EOJ(0x013001), [0x80, 0xB0], request_timeout=1.0
            )
        )
        await asyncio.sleep(0.01)

        tid = next(iter(client._pending_infs.keys()))
        response = Frame(
            tid=tid,
            seoj=EOJ(0x013001),
            deoj=CONTROLLER_INSTANCE,
            esv=ESV_INF,
            properties=[Property(epc=0x80), Property(epc=0xB0)],
        )
        self._simulate_receive(client, response, "192.168.1.10")

        result = await request_task
        assert result.successful_epcs == frozenset({0x80, 0xB0})
        assert result.failed_epcs == frozenset()
        assert result.unanswered_epcs == frozenset()

    @pytest.mark.asyncio
    async def test_partial_epcs_missing_from_inf_are_unanswered(
        self, client_with_protocol: HemsClient
    ) -> None:
        """EPCs requested but absent from a 0x73 response are unanswered."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        request_task = asyncio.create_task(
            client.request_notifications(
                node_id, EOJ(0x013001), [0x80, 0xB0], request_timeout=1.0
            )
        )
        await asyncio.sleep(0.01)

        tid = next(iter(client._pending_infs.keys()))
        response = Frame(
            tid=tid,
            seoj=EOJ(0x013001),
            deoj=CONTROLLER_INSTANCE,
            esv=ESV_INF,
            properties=[Property(epc=0x80)],  # 0xB0 missing
        )
        self._simulate_receive(client, response, "192.168.1.10")

        result = await request_task
        assert result.successful_epcs == frozenset({0x80})
        assert result.failed_epcs == frozenset()
        assert result.unanswered_epcs == frozenset({0xB0})

    @pytest.mark.asyncio
    async def test_inf_sna_response_marks_epcs_failed(
        self, client_with_protocol: HemsClient
    ) -> None:
        """A 0x53 (INF_SNA) response marks its listed EPCs as failed."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        request_task = asyncio.create_task(
            client.request_notifications(
                node_id, EOJ(0x013001), [0x80, 0xB0], request_timeout=1.0
            )
        )
        await asyncio.sleep(0.01)

        tid = next(iter(client._pending_infs.keys()))
        response = Frame(
            tid=tid,
            seoj=EOJ(0x013001),
            deoj=CONTROLLER_INSTANCE,
            esv=ESV_INF_SNA,
            properties=[Property(epc=0x80), Property(epc=0xB0)],
        )
        self._simulate_receive(client, response, "192.168.1.10")

        result = await request_task
        assert result.successful_epcs == frozenset()
        assert result.failed_epcs == frozenset({0x80, 0xB0})
        assert result.unanswered_epcs == frozenset()

    @pytest.mark.asyncio
    async def test_timeout_marks_all_epcs_unanswered(
        self, client_with_protocol: HemsClient
    ) -> None:
        """No response before the timeout marks all EPCs unanswered."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        result = await client.request_notifications(
            node_id, EOJ(0x013001), [0x80, 0xB0], request_timeout=0.1
        )

        assert result.successful_epcs == frozenset()
        assert result.failed_epcs == frozenset()
        assert result.unanswered_epcs == frozenset({0x80, 0xB0})

    @pytest.mark.asyncio
    async def test_response_from_unexpected_address_ignored(
        self, client_with_protocol: HemsClient
    ) -> None:
        """A same-TID response from a different address does not resolve the pending request."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        request_task = asyncio.create_task(
            client.request_notifications(
                node_id, EOJ(0x013001), [0x80], request_timeout=0.1
            )
        )
        await asyncio.sleep(0.01)

        tid = next(iter(client._pending_infs.keys()))
        response = Frame(
            tid=tid,
            seoj=EOJ(0x013001),
            deoj=CONTROLLER_INSTANCE,
            esv=ESV_INF,
            properties=[Property(epc=0x80)],
        )
        self._simulate_receive(client, response, "10.0.0.99")

        result = await request_task
        assert result.unanswered_epcs == frozenset({0x80})

    @pytest.mark.asyncio
    async def test_no_address_known_returns_unanswered(self) -> None:
        """Requesting notifications for an unknown node returns unanswered."""
        client = HemsClient()
        result = await client.request_notifications(
            "unknown-node", EOJ(0x013001), [0x80, 0xB0]
        )
        assert result.successful_epcs == frozenset()
        assert result.failed_epcs == frozenset()
        assert result.unanswered_epcs == frozenset({0x80, 0xB0})

    @pytest.mark.asyncio
    async def test_empty_epcs_returns_empty_result(
        self, client_with_protocol: HemsClient
    ) -> None:
        """Requesting notifications with no EPCs is a no-op."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        result = await client.request_notifications(node_id, EOJ(0x013001), [])
        assert result.successful_epcs == frozenset()
        assert result.failed_epcs == frozenset()
        assert result.unanswered_epcs == frozenset()
