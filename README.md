# scrypted-lorex-doorbell-py

A [Scrypted](https://www.scrypted.app/) plugin for **Lorex B451AJ** and
compatible Dahua / Skywatch-firmware doorbells. Single artifact —
install one Scrypted plugin, fill in IP + credentials, the doorbell
shows up in HomeKit (or any Scrypted destination) with:

- Button-press **BinarySensor** (HomeKit doorbell ring)
- **MotionSensor**
- RTSP **Camera** + **VideoCamera** (live preview, snapshot)
- **Two-way intercom** via DVRIP (push-to-talk in the Home app)

Runs natively in Scrypted as a Python plugin — no separate systemd
daemon, no SSE bridge, no second config file.

## Why this exists

The Lorex B451AJ ships with **Skywatch firmware**, which refuses the
modern DHIP / JSON protocol that every off-the-shelf Dahua integration
speaks. Skywatch only accepts the legacy DVRIP binary protocol
(0xf6-magic framing, 3DES login). That protocol is implemented in
exactly one open-source library: [mcw0/DahuaConsole](https://github.com/mcw0/DahuaConsole).
This plugin vendors a trimmed copy of DahuaConsole and wraps it in a
Scrypted plugin worker so events flow directly into HomeKit (or any
Scrypted destination) without an intermediate daemon.

## Install

Standard Scrypted plugin install. From a checkout:

```sh
npm install
npm run scrypted-deploy <scrypted-host>:10443
```

Then in the Scrypted UI: open the **Lorex Doorbell** plugin → **Add
Device** → fill in:

| Field | Value |
|---|---|
| Name | e.g. *Front Door* |
| IP | LAN IP of the doorbell |
| Username | `admin` (default) |
| Password | doorbell password |
| DVRIP Port | `35000` (Skywatch) or `37777` (stock Dahua) |

Save. The plugin connects, subscribes to the doorbell's event manager,
and exposes the device on whatever Scrypted destinations you have
configured (HomeKit, Google, etc.). RTSP video URL is auto-derived
from IP + credentials; override is in the device's Advanced settings.

## Tested hardware

- **Lorex B451AJ** (Skywatch firmware) — full feature set including
  two-way intercom.
- Other Dahua-derived doorbells with DVRIP (port 37777) **should** work
  with `DVRIP Port: 37777` and `Protocol: dvrip`, but haven't been
  verified — file an issue if you try one.

## Architecture notes

- **DahuaConsole vendored under `src/lorex/DahuaConsole/`** — MIT
  licensed, original LICENSE preserved.
- **No `pwntools` / `pyOpenSSL` dependencies.** Both transitively pull
  cffi 2.0, which conflicts with the Scrypted runtime's bundled
  `_cffi_backend` 1.16. Replaced with a stdlib-backed shim at
  `src/lorex/DahuaConsole/pwn.py` covering the exact API surface
  DahuaConsole uses (byte pack/unpack helpers, `log` singleton,
  `remote()` tube wrapping a stdlib socket, `PwnlibException` stub,
  stdlib re-exports). pyOpenSSL is replaced by a tiny `OpenSSL.py`
  stub since DahuaConsole only used it in an unreachable
  CertManager-export debug path.
- **Threading model**: DahuaConsole's blocking-socket-with-keepalive
  approach runs inside one outer Python thread (`DvripClient._run`)
  and bridges events back to the asyncio loop via
  `loop.call_soon_threadsafe`.
- **Idempotent press detection**: a real Skywatch press chain emits
  4–5 codes (`BackKeyLight`, `VideoTalk`, `CallNoAnswered`,
  `PhoneCallDetect`) within ~20ms. `triggerPress` only flips the
  BinarySensor on the *first* event in a 3-second window so HomeKit
  fires one notification, not four.
- **Two-way intercom**: HomeKit's RTP audio is transcoded by ffmpeg to
  PCMA at **22.05 kHz mono** (the rate Lorex/Skywatch DVRIP expects,
  despite TalkSession's misleading "PCMA-8k" comments) and pushed
  frame-by-frame into the doorbell speaker via DVRIP type-0x1d packets.

## Development

```sh
npm install
npm run build
npm run scrypted-deploy <scrypted-host>:10443

# debug logs go to the per-plugin Console tab in the Scrypted UI;
# stdlib logging is bridged via src/lorex/logging_bridge.py
```

There's a **Test Press** / **Test Motion** button under the device's
Settings → Debug subgroup for end-to-end verification without anyone
needing to physically press the doorbell.

## Press webhook (optional)

For external integrations — e.g. an NAS-side process that builds an
event clip from continuous recordings whenever the doorbell rings —
fill in **Press Webhook URL** under the device's Advanced settings.
On the *first* event in each press chain (mirroring the HomeKit
notification semantics), the plugin POSTs:

```json
{
  "timestamp": "2026-04-28T15:29:04",
  "deviceId": "<scrypted nativeId>",
  "deviceName": "Front Door",
  "event": "press"
}
```

…to the configured URL with `Content-Type: application/json` and a
3-second timeout. Failures are logged at WARNING and otherwise
ignored — the webhook never affects HomeKit notifications or the
press handler.

## License

[MIT](LICENSE). Includes a vendored copy of [mcw0/DahuaConsole](https://github.com/mcw0/DahuaConsole)
(also MIT, Copyright (c) 2021 bashis) under `src/lorex/DahuaConsole/`.
