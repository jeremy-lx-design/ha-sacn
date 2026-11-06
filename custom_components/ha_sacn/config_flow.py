"""Configuration and patch editor for sACN."""

import ipaddress
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import CONF_INTERFACE, CONF_INTERVAL_MS, CONF_PATCHES, DEFAULT_INTERFACE, DEFAULT_INTERVAL_MS, DOMAIN
from .patch import FOOTPRINTS, Patch


def _settings_schema(settings: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_INTERFACE, default=settings.get(CONF_INTERFACE, DEFAULT_INTERFACE)): str,
            vol.Required(CONF_INTERVAL_MS, default=settings.get(CONF_INTERVAL_MS, DEFAULT_INTERVAL_MS)): vol.All(
                vol.Coerce(int), vol.Range(min=100, max=10000)
            ),
        }
    )


def _valid_interface(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return not address.is_multicast and str(address) != "255.255.255.255"


def _patch_schema(patch: dict[str, Any] | None = None) -> vol.Schema:
    patch = patch or {}
    entity_key = vol.Required("entity_id", default=patch["entity_id"]) if patch else vol.Required("entity_id")
    return vol.Schema(
        {
            entity_key: EntitySelector(EntitySelectorConfig(domain=["light", "switch"])),
            vol.Required("universe", default=patch.get("universe", 1)): vol.All(vol.Coerce(int), vol.Range(min=1, max=63999)),
            vol.Required("address", default=patch.get("address", 1)): vol.All(vol.Coerce(int), vol.Range(min=1, max=512)),
            vol.Required("profile", default=patch.get("profile", "dimmer")): vol.In(list(FOOTPRINTS)),
        }
    )


class SacnConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one receiver per Home Assistant instance."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            if not _valid_interface(user_input[CONF_INTERFACE]):
                errors[CONF_INTERFACE] = "invalid_interface"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="sACN", data={**user_input, CONF_PATCHES: []})
        return self.async_show_form(step_id="user", data_schema=_settings_schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SacnOptionsFlow()


class SacnOptionsFlow(OptionsFlow):
    """Manage network settings and one patch per entity."""

    def __init__(self) -> None:
        self._selected: str | None = None

    def _settings(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self, **updates: Any):
        return self.async_create_entry(title="", data={**self._settings(), **updates})

    async def async_step_init(self, user_input=None):
        choices = ["add", "settings"]
        if self._settings().get(CONF_PATCHES):
            choices.extend(["edit", "remove"])
        return self.async_show_menu(step_id="init", menu_options=choices)

    async def async_step_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            if _valid_interface(user_input[CONF_INTERFACE]):
                return self._save(**user_input)
            errors[CONF_INTERFACE] = "invalid_interface"
        return self.async_show_form(step_id="settings", data_schema=_settings_schema(user_input or self._settings()), errors=errors)

    async def async_step_add(self, user_input=None):
        return await self._patch_form("add", user_input)

    async def async_step_edit(self, user_input=None):
        if user_input is not None:
            self._selected = user_input["entity_id"]
            return await self.async_step_edit_patch()
        return self.async_show_form(step_id="edit", data_schema=self._choose_schema())

    async def async_step_edit_patch(self, user_input=None):
        return await self._patch_form("edit_patch", user_input)

    async def async_step_remove(self, user_input=None):
        if user_input is not None:
            patches = [p for p in self._settings()[CONF_PATCHES] if p["entity_id"] != user_input["entity_id"]]
            return self._save(patches=patches)
        return self.async_show_form(step_id="remove", data_schema=self._choose_schema())

    def _choose_schema(self) -> vol.Schema:
        entities = [p["entity_id"] for p in self._settings().get(CONF_PATCHES, [])]
        return vol.Schema({vol.Required("entity_id"): vol.In(entities)})

    async def _patch_form(self, step: str, user_input: dict[str, Any] | None):
        patches = self._settings().get(CONF_PATCHES, [])
        current = next((p for p in patches if p["entity_id"] == self._selected), None) if step == "edit_patch" else None
        errors = {}
        if user_input is not None:
            try:
                Patch.from_dict(user_input)
            except ValueError:
                errors["base"] = "invalid_patch"
            else:
                entity = user_input["entity_id"]
                if self.hass.states.get(entity) is None:
                    errors["entity_id"] = "unknown_entity"
                elif any(p["entity_id"] == entity and p["entity_id"] != self._selected for p in patches):
                    errors["entity_id"] = "duplicate_entity"
                else:
                    return self._save(patches=[p for p in patches if p["entity_id"] != self._selected] + [user_input])
        return self.async_show_form(step_id=step, data_schema=_patch_schema(user_input or current), errors=errors)
