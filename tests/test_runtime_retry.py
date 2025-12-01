"""Tests for runtime retry logic in HemsClient."""

import asyncio
from unittest.mock import MagicMock

import pytest

from pyhems.frame import Frame, Property
from pyhems.runtime import HemsClient


@pytest.fixture
def client_with_protocol() -> HemsClient:
    """Fixture for HemsClient with mocked protocol."""
    client = HemsClient()
    client._protocol = MagicMock()
    return client


class TestRuntimeRetry:
    """Tests for HemsClient retry logic."""

    @staticmethod
    def _simulate_receive(client: HemsClient, frame: Frame, address: str) -> None:
        """Simulate receiving a frame via _on_receive."""
        frame_data = frame.encode()
        client._on_receive(frame_data, (address, 3610))

    @pytest.mark.asyncio
    async def test_async_get_late_response_retry(
        self, client_with_protocol: HemsClient
    ) -> None:
        """Test async_get accepts late response to initial request after retry sent."""
        client = client_with_protocol
        node_id = "fe00000000000000000000000000000001"
        client._device_addresses.forceput("192.168.1.10", node_id)

        async def run_test() -> list[Property]:
            return await client.async_get(
                node_id, 0x013001, [0x80], request_timeout=0.1
            )

        get_task = asyncio.create_task(run_test())

        # Wait for first request to be sent
        await asyncio.sleep(0.05)
        assert len(client._pending_gets) == 1
        tid1 = next(iter(client._pending_gets.keys()))

        # Wait for timeout and retry
        await asyncio.sleep(0.15)
        # Should still have pending gets
        # Crucially, the TID for the retry should be the SAME as initial

        assert len(client._pending_gets) == 1
        tid2 = next(iter(client._pending_gets.keys()))
        assert tid1 == tid2

        # Now send response to the INITIAL request (which is same TID)
        response = Frame(
            tid=tid1,
            seoj=b"\x01\x30\x01",
            deoj=b"\x05\xff\x01",
            esv=0x72,
            properties=[Property(epc=0x80, edt=b"\x30")],
        )
        self._simulate_receive(client, response, "192.168.1.10")

        result = await get_task
        assert len(result) == 1
        assert result[0].epc == 0x80
        assert result[0].edt == b"\x30"
