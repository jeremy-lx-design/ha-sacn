"""The subset of E1.31 needed for unsynchronized DMX level reception."""

from dataclasses import dataclass
import logging

ACN_ID = b"ASC-E1.17\x00\x00\x00"
SOURCE_TIMEOUT = 2.5
MAX_SOURCES_PER_UNIVERSE = 256
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Packet:
    """A validated E1.31 data packet."""

    cid: bytes
    universe: int
    priority: int
    sequence: int
    terminated: bool
    levels: bytes


def parse_packet(data: bytes) -> Packet | None:
    """Parse ordinary, non-preview, unsynchronized DMX data; reject other PDUs."""
    if len(data) < 126 or data[:4] != b"\x00\x10\x00\x00" or data[4:16] != ACN_ID:
        return None
    for offset in (16, 38, 115):
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length >> 12 != 7 or length & 0xFFF != len(data) - offset:
            return None
    if (
        data[18:22] != b"\x00\x00\x00\x04"
        or data[40:44] != b"\x00\x00\x00\x02"
        or data[117:123] != b"\x02\xa1\x00\x00\x00\x01"
    ):
        return None
    count = int.from_bytes(data[123:125], "big")
    universe = int.from_bytes(data[113:115], "big")
    options = data[112]
    if (
        not 1 <= count <= 513
        or len(data) != 125 + count
        or data[125] != 0
        or not 1 <= universe <= 63999
        or data[108] > 200
        or options & 0x80  # Preview data
        or options & 0x20  # Force synchronization
        or data[109:111] != b"\x00\x00"  # Synchronization not supported
    ):
        return None
    return Packet(data[22:38], universe, data[108], data[111], bool(options & 0x40), data[126:])


@dataclass
class _Source:
    priority: int
    sequence: int
    levels: bytes
    seen: float


class Merger:
    """Track independent sources and HTP-merge those at the highest priority."""

    def __init__(self) -> None:
        self._sources: dict[int, dict[bytes, _Source]] = {}
        self._capacity_warned: set[int] = set()

    def update(self, packet: Packet, now: float) -> bool:
        """Accept a new sequence or termination. Return whether state changed."""
        sources = self._sources.setdefault(packet.universe, {})
        previous = sources.get(packet.cid)
        if previous is not None and now - previous.seen >= SOURCE_TIMEOUT:
            del sources[packet.cid]
            previous = None
        if previous is not None:
            difference = (packet.sequence - previous.sequence + 128) % 256 - 128
            if -20 < difference <= 0:
                return False
        if packet.terminated:
            if previous is None:
                if not sources:
                    self._sources.pop(packet.universe, None)
                return False
            del sources[packet.cid]
            if not sources:
                del self._sources[packet.universe]
            return True
        if previous is None and len(sources) >= MAX_SOURCES_PER_UNIVERSE:
            if packet.universe not in self._capacity_warned:
                _LOGGER.warning("sACN universe %s has reached the %s-source limit", packet.universe, MAX_SOURCES_PER_UNIVERSE)
                self._capacity_warned.add(packet.universe)
            return False
        self._capacity_warned.discard(packet.universe)
        sources[packet.cid] = _Source(packet.priority, packet.sequence, packet.levels, now)
        return True

    def expire(self, now: float) -> set[int]:
        """Remove timed-out sources and return affected universes."""
        changed = set()
        for universe, sources in list(self._sources.items()):
            for cid, source in list(sources.items()):
                if now - source.seen >= SOURCE_TIMEOUT:
                    del sources[cid]
                    changed.add(universe)
            if not sources:
                del self._sources[universe]
            if len(sources) < MAX_SOURCES_PER_UNIVERSE:
                self._capacity_warned.discard(universe)
        return changed

    def levels(self, universe: int) -> bytes | None:
        """Return HTP-merged levels, or None when no source is active."""
        sources = self._sources.get(universe)
        if not sources:
            return None
        priority = max(source.priority for source in sources.values())
        winners = [source.levels for source in sources.values() if source.priority == priority]
        result = bytearray(max(map(len, winners)))
        for levels in winners:
            for index, value in enumerate(levels):
                result[index] = max(result[index], value)
        return bytes(result)


def multicast_address(universe: int) -> str:
    """Return the standard multicast group for a universe."""
    return f"239.255.{universe >> 8}.{universe & 255}"
