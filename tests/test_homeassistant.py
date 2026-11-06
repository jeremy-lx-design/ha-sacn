"""Exercise integration flows against Home Assistant when it is installed."""

import asyncio
import importlib.util
import unittest
from unittest.mock import AsyncMock, MagicMock

HAS_HA = importlib.util.find_spec("homeassistant") is not None
if HAS_HA:
    from custom_components.ha_sacn import async_setup_entry, async_unload_entry
    from custom_components.ha_sacn import receiver as receiver_module
    from custom_components.ha_sacn.config_flow import SacnConfigFlow, SacnOptionsFlow
    from test_core import wire_packet
    import socket


@unittest.skipUnless(HAS_HA, "Home Assistant is not installed")
class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_and_options_forms(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.data = {"interface": "0.0.0.0", "interval_ms": 200, "patches": []}
        entry.options = {}
        hass.config_entries.async_get_known_entry.return_value = entry
        hass.states.get.return_value = object()

        flow = SacnConfigFlow()
        flow.hass = hass
        flow.handler = "ha_sacn"
        flow.flow_id = "config-flow"
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        invalid = await flow.async_step_user({"interface": "not-an-address", "interval_ms": 200})
        self.assertEqual(invalid["errors"]["interface"], "invalid_interface")
        form = await flow.async_step_user()
        data = form["data_schema"]({"interface": "0.0.0.0", "interval_ms": 200})
        configured = await flow.async_step_user(data)
        self.assertEqual(configured["data"]["patches"], [])

        options = SacnOptionsFlow()
        options.hass = hass
        options.handler = "entry-id"
        options.flow_id = "options-flow"
        menu = await options.async_step_init()
        self.assertEqual(set(menu["menu_options"]), {"add", "settings"})
        form = await options.async_step_add()
        data = form["data_schema"]({"entity_id": "light.stage", "universe": 1, "address": 101, "profile": "rgb"})
        saved = await options.async_step_add(data)
        self.assertEqual(saved["data"]["patches"], [data])
        entry.options = saved["data"]
        duplicate = await options.async_step_add(data)
        self.assertEqual(duplicate["errors"]["entity_id"], "duplicate_entity")
        invalid = await options.async_step_add({**data, "entity_id": "switch.stage"})
        self.assertEqual(invalid["errors"]["base"], "invalid_patch")
        result = await options.async_step_edit({"entity_id": "light.stage"})
        self.assertEqual(result["step_id"], "edit_patch")
        edited = await options.async_step_edit_patch({**data, "address": 10})
        self.assertEqual(edited["data"]["patches"][0]["address"], 10)
        removed = await options.async_step_remove({"entity_id": "light.stage"})
        self.assertEqual(removed["data"]["patches"], [])

    async def test_setup_receives_and_unloads(self):
        hass = MagicMock()
        hass.data = {}
        hass.services.async_call = AsyncMock()
        entry = MagicMock()
        entry.entry_id = "test"
        entry.data = {"interface": "127.0.0.1", "interval_ms": 100, "patches": [
            {"entity_id": "light.stage", "universe": 1, "address": 1, "profile": "dimmer"}
        ]}
        entry.options = {}
        original_port = receiver_module.PORT
        receiver_module.PORT = 0
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.assertTrue(await async_setup_entry(hass, entry))
            receiver = hass.data["ha_sacn"]["test"]
            port = receiver._transport.get_extra_info("sockname")[1]
            sender.sendto(wire_packet(levels=b"\x80"), ("127.0.0.1", port))
            await asyncio.sleep(0.05)
            hass.services.async_call.assert_awaited_once_with(
                "light", "turn_on", {"entity_id": "light.stage", "brightness": 128}, blocking=True
            )
            self.assertTrue(await async_unload_entry(hass, entry))
            self.assertEqual(hass.data["ha_sacn"], {})
        finally:
            sender.close()
            receiver_module.PORT = original_port
