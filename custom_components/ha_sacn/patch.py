"""Translate patched DMX levels to desired Home Assistant service calls."""

from dataclasses import dataclass
from typing import Any

FOOTPRINTS = {"dimmer": 1, "rgb": 3, "dimmer_rgb": 4, "switch": 1}


@dataclass(frozen=True)
class Patch:
    entity_id: str
    universe: int
    address: int
    profile: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Patch":
        patch = cls(data["entity_id"], int(data["universe"]), int(data["address"]), data["profile"])
        if patch.profile not in FOOTPRINTS:
            raise ValueError(f"Unknown patch profile: {patch.profile}")
        expected_domain = "switch" if patch.profile == "switch" else "light"
        if not patch.entity_id.startswith(expected_domain + "."):
            raise ValueError(f"Profile {patch.profile} requires a {expected_domain} entity")
        if not 1 <= patch.universe <= 63999 or not 1 <= patch.address <= 513 - FOOTPRINTS[patch.profile]:
            raise ValueError("Universe or channel footprint out of range")
        return patch


def resolve(patch: Patch, levels: bytes, previous_switch: bool | None) -> tuple[str, str, dict[str, Any]] | None:
    """Resolve a complete footprint. Switches use hysteresis around mid-scale."""
    start = patch.address - 1
    values = levels[start : start + FOOTPRINTS[patch.profile]]
    if len(values) != FOOTPRINTS[patch.profile]:
        return None
    entity = {"entity_id": patch.entity_id}
    if patch.profile == "switch":
        on = values[0] >= (120 if previous_switch else 128)
        return "switch", "turn_on" if on else "turn_off", entity
    if patch.profile == "dimmer":
        brightness = values[0]
        rgb = None
    elif patch.profile == "dimmer_rgb":
        brightness = values[0]
        rgb = tuple(values[1:])
    else:
        brightness = max(values)
        rgb = tuple(round(value * 255 / brightness) for value in values) if brightness else None
    if brightness == 0:
        return "light", "turn_off", entity
    if rgb is not None:
        entity["rgb_color"] = rgb
    entity["brightness"] = brightness
    return "light", "turn_on", entity
