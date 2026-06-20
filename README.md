# neewer-obs-bridge

Bridge OBS Studio scene changes to one or more Neewer 660 Pro RGB panel lights
over Bluetooth Low Energy (BLE). When you switch scenes in OBS, each light
applies its predefined colour/brightness profile for that scene. It can also
take on-demand overrides over HTTP (e.g. from a Stream Deck) without changing the
OBS scene.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- OBS Studio with WebSocket v5 enabled (Tools → WebSocket Server Settings)
- macOS: grant your terminal Bluetooth access
  (System Settings → Privacy & Security → Bluetooth, then restart the terminal)

## Setup

```sh
uv sync
```

## Quick start

```sh
cd /path/to/neewer-obs-bridge

uv run neewer-obs-bridge              # 1st run: writes default config.toml and exits
uv run neewer-obs-bridge --discover   # find each light's BLE address + confirm char_uuid
#   → edit config.toml: set the OBS password and the [lights] addresses
uv run neewer-obs-bridge --init-scenes  # read OBS scene names, scaffold a profile per light
#   → edit the generated [scenes.*] profiles to taste
uv run neewer-obs-bridge              # bridge mode — switch scenes in OBS
```

Stop the bridge with **Ctrl-C**. The bridge reads `config.toml` from the current
directory (or `--config PATH`), so run it from the project folder. Config is read
**only at startup** — restart after editing.

## Commands

```sh
uv run neewer-obs-bridge                       # normal bridge mode
uv run neewer-obs-bridge --discover            # scan BLE, list devices, dump services/UUIDs
uv run neewer-obs-bridge --list-scenes         # ask OBS for its scene names (✓ = has a profile)
uv run neewer-obs-bridge --init-scenes         # append default per-light profiles for missing OBS scenes
uv run neewer-obs-bridge --test-scene "piano"  # send a scene's per-light profiles without OBS
uv run neewer-obs-bridge --test-light left     # send a bright 5600K test to ONE light (to isolate it)
uv run neewer-obs-bridge --config path.toml    # use a specific config file
```

On startup in bridge mode, it verifies that every OBS scene has a profile in the
config and logs a warning for any that don't (those scenes are ignored).

## Configuration

All settings live in `config.toml`: OBS connection, BLE characteristic UUID, the
HTTP control server, the named lights, and the scene map.

### Lights

Give each panel a friendly name → its BLE address (from `--discover`):

```toml
[lights]
left  = "81A6E3DF-4EE3-F30C-8897-BB816C5A9A88"
right = "71121D85-6258-3148-9D12-3849912016EC"
```

On macOS, bleak reports CoreBluetooth UUIDs (stable per Mac), not MAC addresses.

### Scenes

Each scene defines a profile **per light**, so the panels can differ in the same
scene. There are three modes:

| Mode  | Fields                                     | Use                |
| ----- | ------------------------------------------ | ------------------ |
| `CCT` | `brightness` (0–100), `temp` (3200–5600 K) | white, warm↔cool   |
| `RGB` | `r` `g` `b` (0–255), `brightness` (0–100)  | full colour        |
| `OFF` | —                                          | turn the light off |

```toml
[scenes.Gaming.left]
mode = "RGB"
r = 255
g = 20
b = 80
brightness = 90      # brightness controls the level, NOT the R/G/B magnitude

[scenes.Gaming.right]
mode = "CCT"
brightness = 60
temp = 5600
```

Notes:

- A light **omitted** from a scene is left untouched (no command sent to it).
- Scene names with spaces must be quoted in the header:
  `[scenes."Just Chatting".left]`.
- In `RGB` mode the light is driven by hue/saturation (converted from your RGB);
  `brightness` sets the level. `temp` is ignored. Vivid colours render best;
  very desaturated colours look washed out on these panels.
- Temperature range is 3200–5600 K; values outside are clamped.

### HTTP control server (Stream Deck / curl overrides)

When enabled, the running bridge listens on localhost for on-demand overrides
that do **not** change the OBS scene. An override stays until the next OBS scene
change, which re-applies that scene's profile.

```toml
[control]
enabled = true
host = "127.0.0.1"
port = 8765
```

Endpoints (all `GET`):

| Endpoint                             | Effect                               |
| ------------------------------------ | ------------------------------------ |
| `/`                                  | help + list of lights and scenes     |
| `/scene/<name>`                      | apply a configured scene             |
| `/set?mode=cct&brightness=&temp=`    | white override                       |
| `/set?mode=rgb&r=&g=&b=&brightness=` | colour override                      |
| `/off`                               | turn off                             |
| add `&light=left` (or `right`)       | target one light; omit to affect all |

Examples:

```sh
curl "http://127.0.0.1:8765/set?mode=rgb&r=255&g=0&b=0&brightness=100"
curl "http://127.0.0.1:8765/scene/piano"
curl "http://127.0.0.1:8765/off?light=left"
```

#### Wiring it to a Stream Deck

The Stream Deck can't make HTTP requests out of the box. Two ways:

1. **HTTP-request plugin** (cleanest): install "Web Requests" / "HTTP Request"
   (BarRaider) from the Marketplace, set a button to method **GET** and the URL
   above.
2. **Run `curl`**: with a command/launcher plugin, run `/usr/bin/curl` with the
   URL as the argument.

For reusable buttons, define extra scenes in the config that aren't bound to OBS
and point a button at `/scene/<name>` (URL-encode spaces as `%20`):

```toml
[scenes."Flash Rojo".left]
mode = "RGB"
r = 255
g = 0
b = 0
brightness = 100
[scenes."Flash Rojo".right]
mode = "RGB"
r = 255
g = 0
b = 0
brightness = 100
```

→ button URL: `http://127.0.0.1:8765/scene/Flash%20Rojo`

### OBS connection

```toml
[obs]
host = "localhost"
port = 4455
password = ""   # the WebSocket password from OBS, or "" if auth is disabled
```

## Troubleshooting

- **Light connects but doesn't react**: the bridge uses acknowledged writes and
  the verified RGB660 PRO packet format; if one unit still ignores commands, run
  `--test-light <name>` to isolate it, and `--discover` to confirm the write
  characteristic `69400002-…` is present with `write-without-response`.
- **One of two identical lights fails**: confirm both addresses with `--discover`
  and use `--test-light left` / `--test-light right` to check each maps to a
  different physical panel.
- **`Connection refused` to OBS**: enable the WebSocket server in OBS and check
  the port/password in `[obs]`.
