"""Tests for the UDP transport helpers."""

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyhems import check_interface
from pyhems.transport import create_multicast_socket


@pytest.mark.asyncio
async def test_create_multicast_socket_closes_socket_on_failure() -> None:
    """The socket is closed when joining the multicast group fails."""
    sock = MagicMock(spec=socket.socket)
    error = OSError("no such device")

    def _setsockopt(_level: int, option: int, _value: object) -> None:
        if option == socket.IP_ADD_MEMBERSHIP:
            raise error

    sock.setsockopt.side_effect = _setsockopt

    with (
        patch("pyhems.transport.socket.socket", return_value=sock),
        pytest.raises(OSError, match="no such device") as exc_info,
    ):
        await create_multicast_socket("192.0.2.1", MagicMock())

    assert exc_info.value is error
    sock.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_check_interface_opens_and_closes_socket() -> None:
    """check_interface opens the multicast socket and closes it again."""
    protocol = MagicMock()

    with patch(
        "pyhems.transport.create_multicast_socket",
        new_callable=AsyncMock,
        return_value=protocol,
    ) as mock_create:
        await check_interface("192.0.2.1")

    assert mock_create.await_args is not None
    assert mock_create.await_args.args[0] == "192.0.2.1"
    protocol.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_check_interface_propagates_os_error() -> None:
    """check_interface raises OSError when the socket cannot be opened."""
    with (
        patch(
            "pyhems.transport.create_multicast_socket",
            new_callable=AsyncMock,
            side_effect=OSError("address not available"),
        ),
        pytest.raises(OSError, match="address not available"),
    ):
        await check_interface("192.0.2.1")
