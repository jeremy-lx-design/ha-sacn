"""Tests for protocol arbitration, patching, and async output."""

import asyncio
import errno
import importlib
from pathlib import Path
import socket
import sys
import types
import unittest

# Core modules do not require a running Home Assistant instance.
package = types.ModuleType("sacn_test")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "ha_sacn")]
sys.modules["sacn_test"] = package
protocol = importlib.import_module("sacn_test.protocol")
patch_module = importlib.import_module("sacn_test.patch")
output_module = importlib.import_module("sacn_test.output")
receiver_module = importlib.import_module("sacn_test.receiver")


def wire_packet(*, cid=b"a" * 16, universe=1, priority=100, sequence=0, levels=b"\x80", options=0, sync=0, start=0):
    """Build a complete E1.31 data PDU for testing."""
    data = bytearray(126 + len(levels))
    data[:4] = b"\x00\x10\x00\x00"
    data[4:16] = protocol.ACN_ID
    for offset in (16, 38, 115):
        data[offset:offset + 2] = (0x7000 | (len(data) - offset)).to_bytes(2, "big")
    data[18:22] = b"\0\0\0\4"
    data[22:38] = cid
    data[40:44] = b"\0\0\0\2"
    data[108] = priority
    data[109:111] = sync.to_bytes(2, "big")
    data[111] = sequence
    data[112] = options
    data[113:115] = universe.to_bytes(2, "big")
    data[117:123] = b"\x02\xa1\x00\x00\x00\x01"
    data[123:125] = (len(levels) + 1).to_bytes(2, "big")
    data[125] = start
    data[126:] = levels
    return bytes(data)


class ProtocolTests(unittest.TestCase):
    def test_packet_validation_and_unsupported_features(self):
        data = wire_packet(levels=b"\x00\x7f\xff", universe=63999, priority=200)
        packet = protocol.parse_packet(data)
        self.assertEqual((packet.universe, packet.priority, packet.levels), (63999, 200, b"\x00\x7f\xff"))
        for invalid in (
            data[:-1], data + b"\x00", b"x" + data[1:], wire_packet(options=0x80),
            wire_packet(options=0x20), wire_packet(sync=1), wire_packet(start=0xdd),
            wire_packet(universe=0), wire_packet(priority=201), wire_packet(levels=b"x" * 513),
        ):
            with self.subTest(data=invalid[:20]):
                self.assertIsNone(protocol.parse_packet(invalid))
        for offset in (16, 38, 115, 117, 119, 121):
            corrupted = bytearray(data)
            corrupted[offset] ^= 1
            self.assertIsNone(protocol.parse_packet(bytes(corrupted)), offset)
        self.assertEqual(protocol.multicast_address(63999), "239.255.249.255")

    def test_priority_htp_failover_and_termination(self):
        merger = protocol.Merger()
        def update(cid, seq, priority, levels, now, options=0):
            return merger.update(protocol.parse_packet(wire_packet(cid=cid * 16, sequence=seq, priority=priority, levels=levels, options=options)), now)
        self.assertTrue(update(b"a", 1, 100, b"\x0a\xc8", 0))
        self.assertTrue(update(b"b", 1, 100, b"\x14\x64\x05", 0.1))
        self.assertEqual(merger.levels(1), b"\x14\xc8\x05")
        self.assertTrue(update(b"c", 1, 150, b"\x03\x04", 0.2))
        self.assertEqual(merger.levels(1), b"\x03\x04")
        self.assertTrue(update(b"a", 2, 100, b"\x0a\xc8", 2))
        self.assertTrue(update(b"b", 2, 100, b"\x14\x64\x05", 2))
        self.assertEqual(merger.expire(2.7), {1})
        self.assertEqual(merger.levels(1), b"\x14\xc8\x05")
        self.assertTrue(update(b"a", 3, 100, b"", 2.8, 0x40))
        self.assertEqual(merger.levels(1), b"\x14\x64\x05")
        self.assertEqual(merger.expire(4.5), {1})
        self.assertIsNone(merger.levels(1))

    def test_sequence_is_per_source_and_universe_and_wraps(self):
        merger = protocol.Merger()
        def put(cid, universe, seq, now):
            return merger.update(protocol.parse_packet(wire_packet(cid=cid * 16, universe=universe, sequence=seq)), now)
        self.assertTrue(put(b"a", 1, 255, 0))
        self.assertFalse(put(b"a", 1, 255, 0.1))
        self.assertTrue(put(b"b", 1, 255, 0.1))
        self.assertTrue(put(b"a", 2, 255, 0.1))
        self.assertTrue(put(b"a", 1, 0, 0.2))
        self.assertFalse(put(b"a", 1, 255, 0.25))
        self.assertTrue(put(b"a", 1, 10, 0.3))
        self.assertFalse(put(b"a", 1, 9, 0.4))
        self.assertTrue(put(b"a", 1, 9, 3.0))


class PatchTests(unittest.TestCase):
    def make(self, profile, entity="light.stage", address=1):
        return patch_module.Patch.from_dict({"profile": profile, "entity_id": entity, "universe": 1, "address": address})

    def test_footprint_validation_and_rgb(self):
        for data in (
            {"profile": "rgb", "entity_id": "light.a", "universe": 1, "address": 511},
            {"profile": "switch", "entity_id": "light.a", "universe": 1, "address": 1},
            {"profile": "dimmer", "entity_id": "light.a", "universe": 0, "address": 1},
        ):
            with self.assertRaises(ValueError):
                patch_module.Patch.from_dict(data)
        patch = self.make("rgb", address=2)
        self.assertIsNone(patch_module.resolve(patch, b"\x00\x80", None))
        call = patch_module.resolve(patch, b"\x00\x80\x40\x00", None)
        self.assertEqual(call, ("light", "turn_on", {"entity_id": "light.stage", "brightness": 128, "rgb_color": (255, 128, 0)}))
        self.assertEqual(patch_module.resolve(patch, b"\x00\x00\x00\x00", None)[1], "turn_off")
        self.assertEqual(patch_module.resolve(self.make("dimmer_rgb"), b"\x01\xff\x80\x00", None)[2]["brightness"], 1)

    def test_switch_hysteresis(self):
        patch = self.make("switch", "switch.stage")
        for value, previous, expected in ((127, False, "turn_off"), (128, False, "turn_on"), (120, True, "turn_on"), (119, True, "turn_off")):
            self.assertEqual(patch_module.resolve(patch, bytes([value]), previous)[1], expected)


class OutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_is_throttled_latest_sent_and_duplicates_suppressed(self):
        calls = []
        loop = asyncio.get_running_loop()
        async def send(domain, service, data):
            calls.append((loop.time(), data["brightness"]))
        out = output_module.Output(0.06, send)
        def submit(value):
            out.submit("light.a", ("light", "turn_on", {"entity_id": "light.a", "brightness": value}))
        submit(1)
        await asyncio.sleep(0.01)
        for value in range(2, 20):
            submit(value)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.08)
        self.assertEqual(calls[-1][1], 19)
        self.assertLess(len(calls), 7)
        self.assertTrue(all(b[0] - a[0] >= 0.055 for a, b in zip(calls, calls[1:])))
        count = len(calls)
        submit(19)
        await asyncio.sleep(0.07)
        self.assertEqual(len(calls), count)
        await out.close()

    async def test_failure_retry_invalidation_and_serialization(self):
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()
        async def send(domain, service, data):
            calls.append(data["value"])
            started.set()
            if len(calls) == 1:
                await release.wait()
                raise RuntimeError("device unavailable")
        out = output_module.Output(0.01, send)
        def submit(value):
            out.submit("light.a", ("light", "turn_on", {"value": value}))
        submit(1)
        await started.wait()
        submit(2)
        await asyncio.sleep(0.02)
        self.assertEqual(calls, [1])
        release.set()
        with self.assertLogs("sacn_test.output", level="ERROR"):
            await asyncio.sleep(0.03)
        self.assertEqual(calls, [1, 2])
        out.invalidate("light.a")
        submit(2)
        await asyncio.sleep(0.03)
        self.assertEqual(calls, [1, 2, 2])
        await out.close()

    async def test_signal_loss_discards_pending_call(self):
        calls = []
        async def send(domain, service, data):
            calls.append(data["value"])
        out = output_module.Output(0.1, send)
        out.submit("light.a", ("light", "turn_on", {"value": 1}))
        await asyncio.sleep(0.01)
        out.submit("light.a", ("light", "turn_on", {"value": 2}))
        await asyncio.sleep(0.01)
        out.invalidate("light.a")
        await asyncio.sleep(0.12)
        self.assertEqual(calls, [1])
        await out.close()


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_unicast_datagrams_and_source_termination(self):
        calls = []
        async def send(domain, service, data):
            calls.append((service, data))
        output = output_module.Output(0.01, send)
        patch = patch_module.Patch.from_dict({"entity_id": "light.stage", "universe": 1, "address": 1, "profile": "dimmer"})
        original_port = receiver_module.PORT
        receiver_module.PORT = 0
        receiver = receiver_module.Receiver("127.0.0.1", [patch], output)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            await receiver.start()
            port = receiver._transport.get_extra_info("sockname")[1]
            sender.sendto(wire_packet(levels=b"\x80"), ("127.0.0.1", port))
            await asyncio.sleep(0.05)
            self.assertEqual(calls[-1], ("turn_on", {"entity_id": "light.stage", "brightness": 128}))
            sender.sendto(wire_packet(cid=b"b" * 16, priority=150, levels=b"\x1e"), ("127.0.0.1", port))
            await asyncio.sleep(0.03)
            self.assertEqual(calls[-1][1]["brightness"], 30)
            sender.sendto(wire_packet(sequence=1, levels=b"\xc8"), ("127.0.0.1", port))
            sender.sendto(wire_packet(cid=b"b" * 16, sequence=1, priority=150, options=0x40), ("127.0.0.1", port))
            await asyncio.sleep(0.03)
            self.assertEqual(calls[-1][1]["brightness"], 200)
            sender.sendto(wire_packet(sequence=2, options=0x40), ("127.0.0.1", port))
            await asyncio.sleep(0.03)
            sender.sendto(wire_packet(sequence=3, levels=b"\xc8"), ("127.0.0.1", port))
            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 4)
        finally:
            sender.close()
            await receiver.stop()
            receiver_module.PORT = original_port

    async def test_real_multicast_datagram(self):
        calls = []
        async def send(domain, service, data):
            calls.append((service, data))
        patch = patch_module.Patch.from_dict({"entity_id": "light.stage", "universe": 1, "address": 1, "profile": "dimmer"})
        original_port = receiver_module.PORT
        receiver_module.PORT = 0
        receiver = receiver_module.Receiver("127.0.0.1", [patch], output_module.Output(0.01, send))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            await receiver.start()
            port = receiver._transport.get_extra_info("sockname")[1]
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            try:
                sender.sendto(wire_packet(levels=b"\x40"), (protocol.multicast_address(1), port))
            except OSError as err:
                if err.errno in (errno.EPERM, errno.EACCES):
                    self.skipTest("Environment prohibits multicast transmission")
                raise
            await asyncio.sleep(0.05)
            self.assertEqual(calls[-1], ("turn_on", {"entity_id": "light.stage", "brightness": 64}))
        finally:
            sender.close()
            await receiver.stop()
            receiver_module.PORT = original_port


if __name__ == "__main__":
    unittest.main()
