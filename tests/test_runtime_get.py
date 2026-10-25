"""Tests for the GET batching engine in HemsClient (ADR-0001, 0003, 0004)."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyhems import EOJ, ESV, GetAbortedError
from pyhems.frame import Property
from pyhems.runtime import HemsClient, _GetCapability, _GetResponse

_NODE_ID = "fe00000000000000000000000000000001"
_ADDRESS = "192.168.1.10"
_EOJ = EOJ(0x013001)
_MANUFACTURER = Property(epc=0x8A, edt=b"\x00\x00\x01")

# Returns None for no response, or the properties of a 0x72 response.
Responder = Callable[[list[int]], list[Property] | None]


class _FakeDevice:
    """Replace HemsClient._attempt_get_request with a scripted device."""

    def __init__(self, responder: Responder, *, sent: bool = True) -> None:
        self.requests: list[list[int]] = []
        self._responder = responder
        self._sent = sent

    async def __call__(
        self,
        _address: str,
        _deoj: EOJ,
        _seoj: EOJ,
        epcs: list[int],
        _request_timeout: float,
    ) -> tuple[bool, _GetResponse | None]:
        self.requests.append(list(epcs))
        if not self._sent:
            return False, None
        if epcs == [0x8A]:
            return True, _GetResponse(esv=ESV.GET_RES, properties=(_MANUFACTURER,))
        properties = self._responder(list(epcs))
        if properties is None:
            return True, None
        return True, _GetResponse(esv=ESV.GET_RES, properties=tuple(properties))

    @property
    def sizes(self) -> list[int]:
        """Request sizes, excluding the standalone manufacturer read."""
        return [len(epcs) for epcs in self.requests if epcs != [0x8A]]


def _values(epcs: list[int]) -> list[Property]:
    return [Property(epc=epc, edt=bytes([epc])) for epc in epcs]


def _empty(epcs: list[int]) -> list[Property]:
    return [Property(epc=epc, edt=b"") for epc in epcs]


def _answer_up_to(limit: int) -> Responder:
    """Answer requests of at most ``limit`` EPCs, ignore larger ones."""
    return lambda epcs: _values(epcs) if len(epcs) <= limit else None


def _make_client(
    device: _FakeDevice, capability: _GetCapability | None = None
) -> HemsClient:
    client = HemsClient()
    client._protocol = MagicMock()
    client._device_addresses.forceput(_ADDRESS, _NODE_ID)
    client._attempt_get_request = AsyncMock(side_effect=device.__call__)  # type: ignore[method-assign]
    if capability is not None:
        client._get_capabilities[(_NODE_ID, _EOJ)] = capability
    return client


def _limit(client: HemsClient, eoj: EOJ = _EOJ) -> int | None:
    return client.get_observed_batch_capacity(_NODE_ID, eoj)


_EPCS_27 = list(range(0xC0, 0xC0 + 27))


class TestSetupGet:
    """Tests for HemsClient.setup_get."""

    @pytest.mark.asyncio
    async def test_full_response_needs_one_request(self) -> None:
        """A device answering every EPC is read with one request."""
        device = _FakeDevice(_values)
        client = _make_client(device)

        result = await client.setup_get(_NODE_ID, _EOJ, [0x8A, *_EPCS_27])

        assert device.requests[0] == [0x8A]
        assert device.sizes == [27]
        assert [prop.epc for prop in result] == [0x8A, *_EPCS_27]
        assert all(prop.edt for prop in result)
        assert _limit(client) == 27

    @pytest.mark.asyncio
    async def test_timeouts_halve_until_single_epc_answers(self) -> None:
        """27 -> 14 -> 7 -> 4 -> 2 -> 1, then one EPC at a time: 32 requests."""
        device = _FakeDevice(_answer_up_to(1))
        client = _make_client(device)

        result = await client.setup_get(_NODE_ID, _EOJ, _EPCS_27)

        assert device.sizes == [27, 14, 7, 4, 2] + [1] * 27
        assert len(device.sizes) == 32
        assert all(prop.edt for prop in result)
        assert _limit(client) == 1

    @pytest.mark.asyncio
    async def test_all_empty_response_halves_until_values_return(self) -> None:
        """ADR-0001: 48 all PDC=0 -> 24 with values -> limit 24."""
        epcs = list(range(0x90, 0x90 + 48))
        device = _FakeDevice(
            lambda req: _values(req) if len(req) <= 24 else _empty(req)
        )
        client = _make_client(device)

        result = await client.setup_get(_NODE_ID, _EOJ, epcs)

        assert device.sizes == [48, 24, 24]
        assert all(prop.edt for prop in result)
        assert _limit(client) == 24

    @pytest.mark.asyncio
    async def test_all_empty_response_halves_again(self) -> None:
        """Halving does not fix the limit; an all-empty half is halved again."""
        epcs = list(range(0x90, 0x90 + 8))
        device = _FakeDevice(lambda req: _values(req) if len(req) <= 2 else _empty(req))
        client = _make_client(device)

        await client.setup_get(_NODE_ID, _EOJ, epcs)

        assert device.sizes == [8, 4, 2, 2, 2, 2]
        assert _limit(client) == 2

    @pytest.mark.asyncio
    async def test_single_empty_epc_while_probing_fixes_limit_to_one(self) -> None:
        """A single-EPC PDC=0 response while probing is a valid answer."""
        device = _FakeDevice(lambda req: _empty(req) if len(req) == 1 else None)
        client = _make_client(device)

        result = await client.setup_get(_NODE_ID, _EOJ, [0xC0, 0xC1, 0xC2, 0xC3])

        assert device.sizes == [4, 2, 1, 1, 1, 1]
        assert all(not prop.edt for prop in result)
        assert _limit(client) == 1

    @pytest.mark.asyncio
    async def test_all_empty_with_known_limit_means_unsupported(self) -> None:
        """With a known limit, an all-empty response follows the spec."""
        device = _FakeDevice(_empty)
        client = _make_client(
            device,
            _GetCapability(manufacturer_code=1, observed_batch_capacity=4),
        )

        result = await client.setup_get(_NODE_ID, _EOJ, [0xC0, 0xC1, 0xC2, 0xC3])

        assert device.sizes == [4]
        assert all(not prop.edt for prop in result)
        assert _limit(client) == 4

    @pytest.mark.asyncio
    async def test_manufacturer_read_alone_does_not_fix_limit(self) -> None:
        """The standalone 0x8A read is not used to determine the limit."""
        device = _FakeDevice(_values)
        client = _make_client(device)

        result = await client.setup_get(_NODE_ID, _EOJ, [0x8A])

        assert device.requests == [[0x8A]]
        assert result == [_MANUFACTURER]
        assert _limit(client) is None

    @pytest.mark.asyncio
    async def test_single_epc_without_probing_does_not_fix_limit(self) -> None:
        """A one-EPC request sent before any probing does not fix the limit."""
        device = _FakeDevice(_values)
        client = _make_client(device)

        await client.setup_get(_NODE_ID, _EOJ, [0x8A, 0xC0])

        assert device.sizes == [1]
        assert _limit(client) is None

    @pytest.mark.asyncio
    async def test_partial_response_fixes_limit_to_response_opc(self) -> None:
        """A spec-compliant partial response fixes the limit to its OPC."""
        epcs = list(range(0xC0, 0xC8))
        device = _FakeDevice(lambda req: _values(req[:3]))
        client = _make_client(device)

        result = await client.setup_get(_NODE_ID, _EOJ, epcs)

        assert device.requests[1:] == [epcs, epcs[3:6], epcs[6:]]
        assert all(prop.edt for prop in result)
        assert _limit(client) == 3

    @pytest.mark.asyncio
    async def test_timeout_after_known_limit_halves_and_shrinks(self) -> None:
        """A timeout at the known limit halves and shrinks it on response."""
        device = _FakeDevice(_answer_up_to(2))
        client = _make_client(
            device,
            _GetCapability(manufacturer_code=1, observed_batch_capacity=4),
        )

        await client.setup_get(_NODE_ID, _EOJ, list(range(0xC0, 0xC6)))

        assert device.sizes == [4, 2, 2, 2]
        assert _limit(client) == 2

    @pytest.mark.asyncio
    async def test_short_tail_does_not_shrink_limit(self) -> None:
        """A tail batch smaller than the limit does not shrink it."""
        device = _FakeDevice(_answer_up_to(14))
        client = _make_client(device)

        await client.setup_get(_NODE_ID, _EOJ, _EPCS_27)

        assert device.sizes == [27, 14, 13]
        assert _limit(client) == 14

    @pytest.mark.asyncio
    async def test_unrelated_response_is_treated_as_timeout(self) -> None:
        """A response with none of the requested EPCs is split like a timeout."""
        device = _FakeDevice(
            lambda req: _values(req) if len(req) == 1 else _values([0x80])
        )
        client = _make_client(device)

        await client.setup_get(_NODE_ID, _EOJ, [0xC0, 0xC1])

        assert device.sizes == [2, 1, 1]
        assert _limit(client) == 1

    @pytest.mark.asyncio
    async def test_single_epc_timeout_aborts_device(self) -> None:
        """A single-EPC timeout gives up the device without further requests."""
        device = _FakeDevice(
            lambda req: None if len(req) > 1 or req == [0xC1] else _values(req)
        )
        client = _make_client(device)

        with pytest.raises(GetAbortedError):
            await client.setup_get(_NODE_ID, _EOJ, [0xC0, 0xC1, 0xC2, 0xC3])

        assert device.sizes == [4, 2, 1, 1]
        assert _limit(client) == 1

    @pytest.mark.asyncio
    async def test_manufacturer_timeout_aborts_device(self) -> None:
        """An unanswered standalone 0x8A read gives up the device."""
        device = _FakeDevice(_values)

        client = _make_client(device)
        request = AsyncMock(return_value=(True, None))
        client._attempt_get_request = request  # type: ignore[method-assign]

        with pytest.raises(GetAbortedError):
            await client.setup_get(_NODE_ID, _EOJ, [0x8A, 0xC0])

        request.assert_awaited_once_with(_ADDRESS, _EOJ, EOJ(0x05FF01), [0x8A], 30.0)
        assert _limit(client) is None

    @pytest.mark.asyncio
    async def test_send_failure_aborts_device(self) -> None:
        """A request that cannot be sent gives up the device."""
        device = _FakeDevice(_values, sent=False)
        client = _make_client(
            device, _GetCapability(manufacturer_code=1, observed_batch_capacity=None)
        )

        with pytest.raises(GetAbortedError):
            await client.setup_get(_NODE_ID, _EOJ, [0xC0, 0xC1])

        assert device.sizes == [2]
        assert _limit(client) is None

    @pytest.mark.asyncio
    async def test_unknown_address_aborts_device(self) -> None:
        """A device without a known address is given up."""
        device = _FakeDevice(_values)
        client = _make_client(device)
        client._device_addresses.clear()

        with pytest.raises(GetAbortedError):
            await client.setup_get(_NODE_ID, _EOJ, [0xC0])

        assert device.requests == []

    @pytest.mark.asyncio
    async def test_policy_is_kept_while_halving(self) -> None:
        """ADR-0003: 0xB3 and 0xB7 are never requested together, even when split."""
        eoj = EOJ(0x028701)
        epcs = [0xB3, 0xB7, 0xC0, 0xC1, 0xC2, 0xC3]
        device = _FakeDevice(_answer_up_to(2))
        client = _make_client(device)
        client._get_capabilities[(_NODE_ID, eoj)] = _GetCapability(
            manufacturer_code=0x00000B
        )

        result = await client.setup_get(_NODE_ID, eoj, epcs)

        assert all(not {0xB3, 0xB7} <= set(req) for req in device.requests)
        assert all(prop.edt for prop in result)
        assert _limit(client, eoj) == 2


class TestGet:
    """Tests for the public HemsClient.get using the same engine."""

    @pytest.mark.asyncio
    async def test_get_uses_known_limit(self) -> None:
        """Get splits requests by the limit learned during setup."""
        device = _FakeDevice(_values)
        client = _make_client(
            device, _GetCapability(manufacturer_code=1, observed_batch_capacity=2)
        )

        result = await client.get(_NODE_ID, _EOJ, [0xC0, 0xC1, 0xC2])

        assert device.sizes == [2, 1]
        assert all(prop.edt for prop in result)

    @pytest.mark.asyncio
    async def test_get_does_not_probe_on_timeout(self) -> None:
        """Get neither retries nor splits an unanswered request."""
        device = _FakeDevice(lambda _req: None)
        client = _make_client(device)

        result = await client.get(_NODE_ID, _EOJ, [0xC0, 0xC1, 0xC2])

        assert device.sizes == [3]
        assert result == _empty([0xC0, 0xC1, 0xC2])
        assert _limit(client) is None

    @pytest.mark.asyncio
    async def test_get_does_not_learn_from_responses(self) -> None:
        """Get requests omitted EPCs again but does not learn the limit."""
        device = _FakeDevice(lambda req: _values(req[:2]))
        client = _make_client(device)

        result = await client.get(_NODE_ID, _EOJ, [0xC0, 0xC1, 0xC2])

        assert device.requests == [[0xC0, 0xC1, 0xC2], [0xC2]]
        assert all(prop.edt for prop in result)
        assert _limit(client) is None

    @pytest.mark.asyncio
    async def test_get_does_not_split_all_empty_response(self) -> None:
        """Get treats an all-empty response as unsupported EPCs."""
        device = _FakeDevice(_empty)
        client = _make_client(device)

        result = await client.get(_NODE_ID, _EOJ, [0xC0, 0xC1])

        assert device.sizes == [2]
        assert result == _empty([0xC0, 0xC1])
        assert _limit(client) is None
