"""sACN receiver for Home Assistant."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_INTERFACE, CONF_INTERVAL_MS, CONF_PATCHES, DEFAULT_INTERFACE, DEFAULT_INTERVAL_MS, DOMAIN
from .output import Output
from .patch import Patch
from .receiver import Receiver


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the receiver and its patches."""
    settings = {**entry.data, **entry.options}
    try:
        patches = [Patch.from_dict(item) for item in settings.get(CONF_PATCHES, [])]
    except (KeyError, TypeError, ValueError) as err:
        raise ConfigEntryNotReady(f"Invalid sACN patch: {err}") from err
    entities = [patch.entity_id for patch in patches]
    if len(entities) != len(set(entities)):
        raise ConfigEntryNotReady("Each entity may have only one sACN patch")

    async def call(domain: str, service: str, data: dict) -> None:
        await hass.services.async_call(domain, service, data, blocking=True)

    output = Output(int(settings.get(CONF_INTERVAL_MS, DEFAULT_INTERVAL_MS)) / 1000, call)
    receiver = Receiver(settings.get(CONF_INTERFACE, DEFAULT_INTERFACE), patches, output)
    try:
        await receiver.start()
    except OSError as err:
        raise ConfigEntryNotReady(f"Cannot start sACN receiver: {err}") from err
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = receiver
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop receiving and discard pending commands."""
    receiver: Receiver = hass.data[DOMAIN].pop(entry.entry_id)
    await receiver.stop()
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
