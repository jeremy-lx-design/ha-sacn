# sACN for Home Assistant

An early-stage, local sACN (ANSI E1.31) receiver that patches DMX channels to Home Assistant lights and switches. It is intended for experimenting with show control, **not safety-critical use or real-time lighting**. Home Assistant devices, especially cloud or wireless devices, may have substantial latency.

## Install

For now, copy `custom_components/ha_sacn` into Home Assistant's `config/custom_components/ha_sacn` directory and restart Home Assistant. Once a GitHub release is published, the repository can also be added to HACS as a custom integration repository.

Go to **Settings → Devices & services → Add integration → sACN**. Enter the IPv4 address of the interface on the show network, or `0.0.0.0` to use the OS default for multicast. Open **Configure** on the integration to add, edit and remove patches or change the output interval. There is one receiver per Home Assistant instance; changes reload it.

The receiver listens for unicast and joins the multicast group for each patched universe on UDP port **5568**. Firewalls and network switches must allow the traffic; multicast may need an IGMP querier. In container deployments, multicast and UDP 5568 must reach Home Assistant's network namespace; host networking is commonly needed. A specific interface address must exist in that namespace.

## Patching

Each entity has at most one patch. Choose the universe (1–63999), 1-based starting address (1–512), and profile:

| Profile | Entity | Channels |
|---|---|---|
| `dimmer` | light | Dimmer |
| `rgb` | color light | Red, green, blue |
| `dimmer_rgb` | color light | Dimmer, red, green, blue |
| `switch` | switch | Off below 120; on at 128 or above, with hysteresis in between |

For example, universe 1, address 101, `dimmer_rgb` uses addresses 101–104. Patches may overlap to drive multiple entities from the same channels. Choose a light that supports the selected brightness and/or RGB attributes; the integration cannot make an unsupported device support them. The `rgb` profile derives brightness from the highest channel and normalizes color, so RGB intensity controls the light's brightness. A zero dimmer, or an all-zero RGB value, turns the light off.

## Priority and output

Each universe tracks sources independently by CID. The highest active **universe priority** (0–200) wins; equal-priority sources are merged highest-takes-precedence **per DMX slot**. Sources expire after 2.5 seconds without an accepted packet or when they terminate the stream, and lower-priority sources then take over. Preview packets, malformed packets, unsupported start codes, and out-of-order sequences are ignored. Sequence tracking is independent per source and universe.

Only changed desired values are sent. The configurable per-entity interval (100–10000 ms, default 200 ms) throttles service calls, keeps the newest pending value during continuous traffic, and serializes calls per entity. A failed service call is logged and not counted as successful; another incoming frame retries it at the configured rate. Switches use hysteresis to avoid toggling around the threshold.

On complete signal loss, or when a packet does not contain a patch's full footprint, the last physical state is **held**, pending output is discarded, and the next valid level is sent again. An already-dispatched service call may still finish. No blackout or restore command is sent on unload. Manual HA changes are not treated as an sACN priority or automatically restored if the console's value remains unchanged.

## Limitations

- Per-address priority (PAP), sACN synchronization, universe discovery, RDM, live source monitoring, and a dedicated dashboard are **not implemented**. PAP packets are ignored; send ordinary levels with universe priority. Synchronized data is ignored rather than applied out of sync.
- sACN is unauthenticated. Anyone able to send packets to this receiver can control patched entities, including with a higher priority. Use a trusted, isolated show network and firewall.
- Home Assistant service calls and device state changes are not frame-synchronous. Do not rely on this integration for emergencies, safety systems, or critical show cues.

## Development

Run `python -m unittest discover -s tests -v`. The protocol and output tests require only the Python standard library. The Home Assistant flow and lifecycle tests run when Home Assistant is installed (tested with 2025.1.4 on Python 3.12); otherwise they are skipped. The multicast loopback test is skipped if the environment forbids multicast transmission.

Licensed under [MIT](LICENSE).
