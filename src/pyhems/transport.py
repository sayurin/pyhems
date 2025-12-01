"""UDP transport client for ECHONET Lite."""

import asyncio
import logging
import socket
import struct
from collections.abc import Callable

from .const import ECHONET_MULTICAST, ECHONET_PORT

_LOGGER = logging.getLogger(__name__)


class EchonetLiteProtocol(asyncio.DatagramProtocol):
    """UDP protocol for ECHONET Lite communication."""

    def __init__(
        self,
        callback: Callable[[bytes, tuple[str, int]], None],
    ) -> None:
        """Initialize the transport."""
        self._callback = callback
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Handle connection established."""
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming datagram."""
        self._callback(data, addr)

    def error_received(self, exc: Exception) -> None:
        """Handle error."""
        _LOGGER.error("UDP transport error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle connection lost."""
        if exc:
            _LOGGER.warning("UDP transport connection lost: %s", exc)

    def send(self, data: bytes, address: str) -> None:
        """Send data to the specified address."""
        if self._transport:
            self._transport.sendto(data, (address, ECHONET_PORT))

    def close(self) -> None:
        """Close the underlying transport connection."""
        if self._transport:
            self._transport.close()
            self._transport = None


async def create_multicast_socket(
    interface: str,
    callback: Callable[[bytes, tuple[str, int]], None],
) -> EchonetLiteProtocol:
    """Create a UDP socket bound to the ECHONET Lite multicast group.

    Args:
        interface: Local interface IP address to bind to.
        callback: Optional callback for received data.

    Returns:
        EchonetLiteProtocol instance for sending and receiving.

    """
    loop = asyncio.get_running_loop()

    # Create socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Allow multiple sockets to use the same port
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

    # Bind to all interfaces on ECHONET port
    sock.bind(("", ECHONET_PORT))

    # Join multicast group on specified interface
    mreq = struct.pack(
        "4s4s",
        socket.inet_aton(ECHONET_MULTICAST),
        socket.inet_aton(interface),
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    # Set outgoing interface for multicast
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)
    )

    # Set multicast TTL
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)

    # Set non-blocking
    sock.setblocking(False)

    _transport, protocol = await loop.create_datagram_endpoint(
        lambda: EchonetLiteProtocol(callback), sock=sock
    )
    return protocol
