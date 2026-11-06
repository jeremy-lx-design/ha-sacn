"""Async UDP receiver and patch orchestration."""

import asyncio
import logging
import socket

from .const import PORT
from .output import Output
from .patch import Patch, resolve
from .protocol import Merger, multicast_address, parse_packet

_LOGGER = logging.getLogger(__name__)


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, receiver: "Receiver") -> None:
        self._receiver = receiver

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._receiver.receive(data)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("sACN UDP socket error: %s", exc)


class Receiver:
    """Receive subscribed universes, merge sources and apply patches."""

    def __init__(self, interface: str, patches: list[Patch], output: Output) -> None:
        self._interface = interface
        self._patches = patches
        self._universes = {patch.universe for patch in patches}
        self._output = output
        self._merger = Merger()
        self._switches: dict[str, bool] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._expiry_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Bind the unicast port and join the selected multicast groups."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", PORT))
            for universe in self._universes:
                membership = socket.inet_aton(multicast_address(universe)) + socket.inet_aton(self._interface)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setblocking(False)
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Datagrams(self), sock=sock
            )
        except BaseException:
            sock.close()
            raise
        self._transport = transport
        self._expiry_task = asyncio.create_task(self._expire_loop())

    async def stop(self) -> None:
        """Close the socket and cancel outstanding output."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            await asyncio.gather(self._expiry_task, return_exceptions=True)
            self._expiry_task = None
        await self._output.close()

    def receive(self, data: bytes) -> None:
        """Process a datagram from either unicast or multicast."""
        packet = parse_packet(data)
        if packet is None or packet.universe not in self._universes:
            return
        now = asyncio.get_running_loop().time()
        changed = self._merger.expire(now)
        if self._merger.update(packet, now):
            changed.add(packet.universe)
        for universe in changed:
            self._apply(universe)

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            for universe in self._merger.expire(asyncio.get_running_loop().time()):
                self._apply(universe)

    def _apply(self, universe: int) -> None:
        levels = self._merger.levels(universe)
        for patch in self._patches:
            if patch.universe != universe:
                continue
            call = None if levels is None else resolve(patch, levels, self._switches.get(patch.entity_id))
            if call is None:
                self._switches.pop(patch.entity_id, None)
                self._output.invalidate(patch.entity_id)
                continue
            if patch.profile == "switch":
                self._switches[patch.entity_id] = call[1] == "turn_on"
            self._output.submit(patch.entity_id, call)
