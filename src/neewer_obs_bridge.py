#!/usr/bin/env python3
"""
OBS Scene → Neewer 660 Pro RGB bridge (multi-light).

Each OBS scene maps to a per-light profile, so two (or more) Neewer panels can
take different colour/brightness in the same scene (e.g. a key light and a fill
light). Lights are identified by their BLE address, configured in config.toml.

Usage:
    neewer-obs-bridge                       # normal bridge mode
    neewer-obs-bridge --discover            # scan BLE and print all UUIDs
    neewer-obs-bridge --list-scenes         # ask OBS for its scene names
    neewer-obs-bridge --test-scene "Gaming" # send a scene's profiles without OBS
    neewer-obs-bridge --config path.toml    # use a specific config file

Setup:
    1. Enable OBS WebSocket: Tools → WebSocket Server Settings
    2. Run once to generate config.toml, then edit it (OBS password, lights, scenes).
    3. Run --discover to confirm each light's address and characteristic UUID.
    4. Run in normal mode and switch scenes in OBS.
"""

import argparse
import asyncio
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import obsws_python as obs
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter version
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path("config.toml")  # relative to the current directory

# Written verbatim the first time the script runs without a config file.
DEFAULT_CONFIG_TOML = """\
# neewer-obs-bridge configuration

[obs]
host = "localhost"
port = 4455
password = ""              # leave empty if OBS WebSocket auth is disabled

[ble]
name_prefix = "NEEWER"     # partial name used to highlight matches in --discover
# Characteristic UUID for Neewer RGB660 PRO (community reverse-engineering).
# Run --discover to verify this matches your devices.
char_uuid = "69400002-B5A3-F393-E0A9-E50E24DCCA99"

# ── HTTP control server (Stream Deck / curl overrides) ───────────────────────
# When enabled, the running bridge listens for on-demand overrides without
# changing the OBS scene. Bound to localhost only. Example button command:
#   curl "http://127.0.0.1:8765/set?mode=rgb&r=255&g=0&b=0&brightness=100"
[control]
enabled = true
host = "127.0.0.1"
port = 8765

# ── Lights ───────────────────────────────────────────────────────────────────
# Give each panel a friendly name → its BLE address (from --discover).
# On macOS bleak reports CoreBluetooth UUIDs (stable per Mac), not MAC addresses.
[lights]
left  = "81A6E3DF-4EE3-F30C-8897-BB816C5A9A88"
right = "71121D85-6258-3148-9D12-3849912016EC"

# ── Scene → per-light profiles ───────────────────────────────────────────────
# For each scene, define a profile per light: [scenes.<Scene>.<light>]
# Modes: "CCT" (colour temperature), "RGB" (full colour), "OFF".
# A light omitted from a scene is left untouched (no command is sent to it).
# Scene names with spaces must be quoted: [scenes."Just Chatting".left]

[scenes.Gaming.left]
mode = "RGB"
r = 255
g = 20
b = 80
brightness = 90

[scenes.Gaming.right]
mode = "CCT"
brightness = 60
temp = 5600

[scenes."Just Chatting".left]
mode = "CCT"
brightness = 80
temp = 5600

[scenes."Just Chatting".right]
mode = "CCT"
brightness = 60
temp = 5600

[scenes.BRB.left]
mode = "CCT"
brightness = 10
temp = 3200

[scenes.BRB.right]
mode = "OFF"

[scenes."Starting Soon".left]
mode = "CCT"
brightness = 50
temp = 4500

[scenes."Starting Soon".right]
mode = "CCT"
brightness = 50
temp = 4500

[scenes.Ending.left]
mode = "OFF"

[scenes.Ending.right]
mode = "OFF"
"""


class Config:
    """Loaded configuration with light-touch validation."""

    def __init__(self, data: dict):
        obs_cfg = data.get("obs", {})
        ble_cfg = data.get("ble", {})
        self.obs_host: str = obs_cfg.get("host", "localhost")
        self.obs_port: int = int(obs_cfg.get("port", 4455))
        self.obs_password: str = obs_cfg.get("password", "")
        self.name_prefix: str = ble_cfg.get("name_prefix", "NEEWER")
        self.char_uuid: str = ble_cfg.get(
            "char_uuid", "69400002-B5A3-F393-E0A9-E50E24DCCA99"
        )
        # name -> BLE address
        self.lights: dict[str, str] = dict(data.get("lights", {}))
        # scene name -> {light name -> profile dict}
        self.scenes: dict[str, dict[str, dict]] = data.get("scenes", {})
        # HTTP control server (for Stream Deck etc.)
        ctrl = data.get("control", {})
        self.control_enabled: bool = bool(ctrl.get("enabled", False))
        self.control_host: str = ctrl.get("host", "127.0.0.1")
        self.control_port: int = int(ctrl.get("port", 8765))


def load_config(path: Path) -> Config:
    """Load config from TOML, creating a default file on first run."""
    if tomllib is None:
        print(
            "TOML support is missing. Use Python 3.11+ or run: pip install tomli",
            file=sys.stderr,
        )
        sys.exit(1)

    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TOML)
        print(f"Created default config at {path}")
        print("Edit it (OBS password, light addresses, scene map) and run again.")
        sys.exit(0)

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as e:
        print(f"Failed to read config {path}: {e}", file=sys.stderr)
        sys.exit(1)

    cfg = Config(data)
    # Note: empty [lights]/[scenes] are allowed here so that bootstrap modes
    # like --init-scenes and --discover can run on a fresh config. Modes that
    # truly need them (the bridge) enforce it themselves.

    # Validate that every scene references only known lights.
    for scene, per_light in cfg.scenes.items():
        for light_name in per_light:
            if light_name not in cfg.lights:
                print(
                    f"Scene '{scene}' references unknown light '{light_name}'. "
                    f"Known lights: {', '.join(cfg.lights)}",
                    file=sys.stderr,
                )
                sys.exit(1)
    return cfg


def commands_for_scene(cfg: Config, scene: str) -> dict[str, list[bytes]]:
    """Return {light_name: [packets]} for the lights defined in a scene."""
    per_light = cfg.scenes.get(scene, {})
    return {name: profile_to_commands(profile) for name, profile in per_light.items()}


# ── Neewer BLE protocol helpers ───────────────────────────────────────────────

def _checksum(data: bytes) -> int:
    """Neewer checksum: sum of all preceding bytes, modulo 256."""
    return sum(data) & 0xFF


def build_cct_command(brightness: int, temp_kelvin: int) -> bytes:
    """
    Build CCT (colour temperature) command — 6 bytes.
        78 87 02 <brightness 0-100> <temp 0x20-0x38 = 3200-5600K> <checksum>
    brightness: 0-100 | temp_kelvin: 3200-5600 → divided by 100 (32-56)
    """
    b = max(0, min(100, brightness))
    t = max(32, min(56, temp_kelvin // 100))
    payload = bytes([0x78, 0x87, 0x02, b, t])
    return payload + bytes([_checksum(payload)])


def _rgb_to_hue_sat(r: int, g: int, b: int) -> tuple[int, int]:
    """Convert 0-255 RGB to (hue 0-360, saturation 0-100)."""
    rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(rf, gf, bf), min(rf, gf, bf)
    delta = mx - mn
    if delta == 0:
        hue = 0.0
    elif mx == rf:
        hue = (60 * ((gf - bf) / delta) + 360) % 360
    elif mx == gf:
        hue = 60 * ((bf - rf) / delta) + 120
    else:
        hue = 60 * ((rf - gf) / delta) + 240
    sat = 0.0 if mx == 0 else (delta / mx) * 100
    return int(round(hue)) % 360, int(round(sat))


def build_rgb_command(r: int, g: int, b: int, brightness: int) -> bytes:
    """
    Build colour (HSI/HSV) command — 8 bytes. The light takes hue/saturation,
    not raw RGB, so RGB is converted; `brightness` is the luminance (0-100).
        78 86 04 <hue_low> <hue_high 0/1> <saturation 0-100> <luminance 0-100> <checksum>
    """
    hue, sat = _rgb_to_hue_sat(r, g, b)
    lum = max(0, min(100, brightness))
    payload = bytes([0x78, 0x86, 0x04, hue & 0xFF, (hue >> 8) & 0xFF, sat, lum])
    return payload + bytes([_checksum(payload)])


def build_power_command(on: bool) -> bytes:
    """Power on (78 81 01 01) or off (78 81 01 02), + checksum."""
    payload = bytes([0x78, 0x81, 0x01, 0x01 if on else 0x02])
    return payload + bytes([_checksum(payload)])


def profile_to_commands(profile: dict) -> list[bytes]:
    """Return the ordered BLE packets to apply a profile.

    Non-OFF profiles are prefixed with a power-on so a light in standby still
    wakes up and accepts the setting; OFF sends a real power-off.
    """
    mode = profile.get("mode", "CCT").upper()
    if mode == "OFF":
        return [build_power_command(False)]
    if mode == "RGB":
        setting = build_rgb_command(
            profile.get("r", 255),
            profile.get("g", 255),
            profile.get("b", 255),
            profile.get("brightness", 80),
        )
    else:
        setting = build_cct_command(
            profile.get("brightness", 80),
            profile.get("temp", 5600),
        )
    return [build_power_command(True), setting]

# ── BLE error handling ────────────────────────────────────────────────────────

def explain_ble_error(e: BaseException) -> None:
    """Print actionable guidance for common BLE failures (esp. macOS perms)."""
    msg = str(e).lower()
    if any(k in msg for k in ("unauthorized", "not authorized", "denied", "permission")):
        print(
            "\nBluetooth permission denied.\n"
            "On macOS, grant your terminal Bluetooth access:\n"
            "  System Settings → Privacy & Security → Bluetooth → enable your\n"
            "  terminal app (Terminal, iTerm, VS Code, …), then restart it.\n"
            f"\nUnderlying error: {e}",
            file=sys.stderr,
        )
    elif "powered" in msg or "poweredoff" in msg or "off" in msg:
        print(
            "\nBluetooth appears to be off. Turn it on and try again.\n"
            f"Underlying error: {e}",
            file=sys.stderr,
        )
    else:
        print(f"\nBLE error: {e}", file=sys.stderr)

# ── BLE light connection (persistent, with reconnect) ─────────────────────────

class NeewerLight:
    """Persistent BLE connection to one Neewer light."""

    def __init__(self, name: str, address: str, char_uuid: str):
        self.name = name
        self.address = address
        self.char_uuid = char_uuid
        self._client: Optional[BleakClient] = None
        self._use_response = True  # prefer acknowledged writes; fall back if unsupported

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _on_disconnect(self, _client: BleakClient) -> None:
        logging.warning("BLE link to '%s' (%s) dropped; will reconnect on next command.",
                        self.name, self.address)

    async def connect(self) -> None:
        client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
        await client.connect()
        # Let the connection/GATT settle before the first write, otherwise the
        # first packet is sometimes dropped on a freshly-opened link.
        await asyncio.sleep(0.25)
        self._client = client

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
            self._client = None

    async def send(self, command: bytes) -> None:
        if self._client is None:
            raise BleakError("not connected")
        # Acknowledged write: the GATT layer confirms delivery (and retransmits),
        # avoiding the silent packet loss of fire-and-forget write-no-response.
        try:
            await self._client.write_gatt_char(
                self.char_uuid, command, response=self._use_response
            )
        except BleakError:
            if not self._use_response:
                raise
            # This unit/characteristic rejected acknowledged writes; fall back.
            logging.info("'%s': acknowledged write unsupported, using write-no-response.",
                         self.name)
            self._use_response = False
            await self._client.write_gatt_char(self.char_uuid, command, response=False)


async def connect_with_backoff(
    light: NeewerLight, base: float = 1.0, max_delay: float = 30.0,
    max_attempts: Optional[int] = None,
) -> bool:
    """Connect, retrying with exponential backoff. Returns True on success.

    With max_attempts=None it retries forever; otherwise it gives up after that
    many tries and returns False.
    """
    delay = base
    attempt = 0
    while True:
        try:
            await light.connect()
            logging.info("Connected to '%s' light at %s", light.name, light.address)
            return True
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            attempt += 1
            if max_attempts is not None and attempt >= max_attempts:
                logging.warning("Could not connect to '%s' (%s); giving up for now.",
                                light.name, e)
                return False
            logging.warning("Connect to '%s' failed (%s); retrying in %.0fs",
                            light.name, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def deliver(light: NeewerLight, packets: list[bytes], scene: str) -> bool:
    """Ensure connected and write the packets in order, reconnecting once on failure."""
    for attempt in range(2):
        try:
            if not light.is_connected:
                await connect_with_backoff(light)
            for i, packet in enumerate(packets):
                if i:
                    await asyncio.sleep(0.08)  # let the light process power-on first
                await light.send(packet)
            logging.info("Applied '%s' profile to '%s'", scene, light.name)
            return True
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            logging.warning("Send to '%s' failed (%s); reconnecting.", light.name, e)
            await light.disconnect()
    logging.error("Giving up on '%s' for scene '%s' after retry.", light.name, scene)
    return False

# ── --discover mode ───────────────────────────────────────────────────────────

async def run_discover(cfg: Config) -> None:
    """List all BLE devices, then dump services/characteristics of each Neewer."""
    print("\n── BLE scan (10s) ──────────────────────────────────────────")
    try:
        devices = await BleakScanner.discover(timeout=10.0)
    except BleakError as e:
        explain_ble_error(e)
        return

    if not devices:
        print("No BLE devices found. Make sure Bluetooth is on and the lights are powered.")
        return

    neewer_addresses = []
    print(f"{'Name':<35} {'Address':<40} {'RSSI'}")
    print("-" * 85)
    for d in sorted(devices, key=lambda x: x.name or ""):
        name = d.name or "(unnamed)"
        rssi = getattr(d, "rssi", "?")
        print(f"{name:<35} {d.address:<40} {rssi}")
        if cfg.name_prefix.lower() in name.lower():
            neewer_addresses.append(d.address)

    if not neewer_addresses:
        print(f"\nNo device with '{cfg.name_prefix}' in name found.")
        print("Check the lights are on and visible over BLE.")
        return

    known = {addr.lower() for addr in cfg.lights.values()}
    for address in neewer_addresses:
        configured = "  (in config ✓)" if address.lower() in known else "  (NOT in config)"
        print(f"\n── Services & Characteristics for {address}{configured} ──")
        try:
            async with BleakClient(address) as client:
                for service in client.services:
                    print(f"\nService: {service.uuid}  ({service.description})")
                    for char in service.characteristics:
                        props = ", ".join(char.properties)
                        print(f"  Char:  {char.uuid}  [{props}]")
                        if char.uuid.lower() == cfg.char_uuid.lower():
                            print("         ↑ matches char_uuid in config ✓")
        except BleakError as e:
            explain_ble_error(e)

    print("\n── Done ────────────────────────────────────────────────────")
    print("Map each address to a friendly name under [lights] in config.toml.")

# ── --list-scenes mode ────────────────────────────────────────────────────────

def run_list_scenes(cfg: Config) -> None:
    """Connect to OBS and print its scene names."""
    try:
        client = obs.ReqClient(
            host=cfg.obs_host, port=cfg.obs_port, password=cfg.obs_password
        )
        resp = client.get_scene_list()
    except Exception as e:  # obsws raises a variety of types on connect failure
        print(
            f"Could not query OBS at {cfg.obs_host}:{cfg.obs_port}: {e}\n"
            "Is OBS running with WebSocket enabled and the password correct?",
            file=sys.stderr,
        )
        sys.exit(1)

    current = getattr(resp, "current_program_scene_name", None)
    # OBS returns scenes newest-first; reverse for top-to-bottom UI order.
    names = [s["sceneName"] for s in reversed(resp.scenes)]
    print("\nOBS scenes (✓ = has profiles in config):")
    for name in names:
        per_light = cfg.scenes.get(name, {})
        marker = "✓" if per_light else " "
        detail = f"  → {', '.join(per_light)}" if per_light else ""
        active = "  ← current" if name == current else ""
        print(f"  [{marker}] {name}{detail}{active}")

# ── --init-scenes mode ────────────────────────────────────────────────────────

def _toml_key(name: str) -> str:
    """Render a TOML table key, quoting it if it isn't a bare key."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def run_init_scenes(cfg: Config, config_path: Path) -> None:
    """Read OBS scene names and append default per-light profiles for any that
    are missing, into the config file (existing entries are left untouched)."""
    if not cfg.lights:
        print("No [lights] defined; add your lights to the config first.",
              file=sys.stderr)
        sys.exit(1)

    try:
        client = obs.ReqClient(
            host=cfg.obs_host, port=cfg.obs_port, password=cfg.obs_password
        )
        resp = client.get_scene_list()
    except Exception as e:
        print(
            f"Could not query OBS at {cfg.obs_host}:{cfg.obs_port}: {e}\n"
            "Is OBS running with WebSocket enabled and the password correct?",
            file=sys.stderr,
        )
        sys.exit(1)

    names = [s["sceneName"] for s in reversed(resp.scenes)]  # OBS UI order

    blocks = []
    added = []
    for scene in names:
        existing = cfg.scenes.get(scene, {})
        for light in cfg.lights:
            if light in existing:
                continue  # don't redefine an existing [scenes.x.light] table
            blocks.append(
                f"[scenes.{_toml_key(scene)}.{_toml_key(light)}]\n"
                "mode = \"CCT\"\n"
                "brightness = 80\n"
                "temp = 5600\n"
            )
            added.append((scene, light))

    if not added:
        print("All OBS scenes already have a profile for every light. Nothing to add.")
        return

    text = "\n# ── Added by --init-scenes (edit these defaults) ──\n\n" + "\n".join(blocks)
    with config_path.open("a") as fh:
        fh.write(text)

    print(f"Added {len(added)} profile(s) to {config_path}:")
    for scene, light in added:
        print(f"  [scenes.{_toml_key(scene)}.{_toml_key(light)}]  (CCT 5600K @ 80%)")
    print("\nEdit those defaults to taste, then restart the bridge.")


# ── --test-scene mode ─────────────────────────────────────────────────────────

async def run_test_scene(cfg: Config, scene_name: str) -> None:
    """Send the per-light profiles for a given scene without OBS."""
    if scene_name not in cfg.scenes:
        available = ", ".join(cfg.scenes.keys())
        print(f"Scene '{scene_name}' not in config scenes.")
        print(f"Available: {available}")
        sys.exit(1)

    cmds = commands_for_scene(cfg, scene_name)
    if not cmds:
        print(f"Scene '{scene_name}' has no light profiles; nothing to send.")
        return

    lights = {name: NeewerLight(name, cfg.lights[name], cfg.char_uuid) for name in cmds}
    print(f"Sending scene '{scene_name}':")
    for name, packets in cmds.items():
        hexed = "  ".join(p.hex(' ').upper() for p in packets)
        print(f"  {name} ({cfg.lights[name]}): {hexed}")

    try:
        await asyncio.gather(*(
            deliver(lights[name], packets, scene_name)
            for name, packets in cmds.items()
        ))
    finally:
        await asyncio.gather(*(light.disconnect() for light in lights.values()))
    print("Done.")


async def run_test_light(cfg: Config, light_name: str) -> None:
    """Send a bright, obvious setting to ONE named light, to isolate it."""
    if light_name not in cfg.lights:
        print(f"Light '{light_name}' not in [lights]. Known: {', '.join(cfg.lights)}")
        sys.exit(1)

    address = cfg.lights[light_name]
    packets = [build_power_command(True), build_cct_command(100, 5600)]
    print(f"Testing light '{light_name}' ({address}) → power on + 5600K @ 100%")
    for p in packets:
        print(f"  {p.hex(' ').upper()}")

    light = NeewerLight(light_name, address, cfg.char_uuid)
    try:
        ok = await connect_with_backoff(light, max_attempts=4)
        if not ok:
            print(f"Could not connect to '{light_name}'. Is it powered and in range?")
            sys.exit(1)
        await deliver(light, packets, f"test:{light_name}")
    finally:
        await light.disconnect()
    print("Done. Did exactly one light go bright white?")

# ── HTTP control server (Stream Deck / curl overrides) ────────────────────────

def _override_commands(cfg: Config, params: dict) -> "tuple[str, dict[str, list[bytes]]]":
    """Build a per-light command map from /set query params.

    `light` is optional → applies to all lights. mode = cct | rgb | off.
    """
    light = params.get("light")
    mode = params.get("mode", "cct").lower()
    if mode == "rgb":
        profile = {
            "mode": "RGB",
            "r": int(params.get("r", 255)),
            "g": int(params.get("g", 255)),
            "b": int(params.get("b", 255)),
            "brightness": int(params.get("brightness", 80)),
        }
    elif mode == "off":
        profile = {"mode": "OFF"}
    else:
        profile = {
            "mode": "CCT",
            "brightness": int(params.get("brightness", 80)),
            "temp": int(params.get("temp", 5600)),
        }
    packets = profile_to_commands(profile)
    targets = [light] if light else list(cfg.lights)
    cmds = {name: packets for name in targets if name in cfg.lights}
    return f"override:{light or 'all'}:{mode}", cmds


def _make_control_handler(cfg: Config, loop: asyncio.AbstractEventLoop,
                          queue: "asyncio.Queue") -> type:
    """Build a request handler bound to the running bridge's loop and queue."""

    def enqueue(label: str, cmds: "dict[str, list[bytes]]") -> None:
        # Same thread-safe hand-off the OBS callback uses.
        loop.call_soon_threadsafe(queue.put_nowait, (label, cmds))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # silence per-request stderr noise
            pass

        def _reply(self, code: int, text: str) -> None:
            body = (text + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.split("/") if p]
            params = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if not parts:
                    self._reply(200,
                        "neewer-obs-bridge control\n"
                        f"Lights: {', '.join(cfg.lights)}\n"
                        f"Scenes: {', '.join(cfg.scenes) or '(none)'}\n\n"
                        "GET /scene/<name>                          apply a configured scene\n"
                        "GET /set?light=&mode=cct&brightness=&temp= white override\n"
                        "GET /set?light=&mode=rgb&r=&g=&b=&brightness= colour override\n"
                        "GET /off[?light=<name>]                    turn off (all or one)\n"
                        "(omit light= to affect all lights)")
                    return
                if parts[0] == "scene" and len(parts) >= 2:
                    name = parts[1]
                    if name not in cfg.scenes:
                        self._reply(404, f"unknown scene '{name}'")
                        return
                    enqueue(f"http:{name}", commands_for_scene(cfg, name))
                    self._reply(200, f"applied scene '{name}'")
                    return
                if parts[0] == "set":
                    label, cmds = _override_commands(cfg, params)
                    if not cmds:
                        self._reply(400, "no valid light; check the light= name")
                        return
                    enqueue(label, cmds)
                    self._reply(200, f"applied {label}")
                    return
                if parts[0] == "off":
                    label, cmds = _override_commands(cfg, {**params, "mode": "off"})
                    enqueue(label, cmds)
                    self._reply(200, f"applied {label}")
                    return
                self._reply(404, "not found; GET / for help")
            except (ValueError, KeyError) as e:
                self._reply(400, f"bad request: {e}")

    return Handler


def start_control_server(cfg: Config, loop: asyncio.AbstractEventLoop,
                         queue: "asyncio.Queue") -> Optional[ThreadingHTTPServer]:
    handler = _make_control_handler(cfg, loop, queue)
    try:
        httpd = ThreadingHTTPServer((cfg.control_host, cfg.control_port), handler)
    except OSError as e:
        logging.error("Could not start control server on %s:%d (%s).",
                      cfg.control_host, cfg.control_port, e)
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    logging.info("Control server on http://%s:%d  (GET / for endpoints)",
                 cfg.control_host, cfg.control_port)
    return httpd


# ── Bridge mode ───────────────────────────────────────────────────────────────

class Bridge:
    """Receives OBS scene-change events and enqueues per-light BLE commands.

    The OBS event callback runs in a worker thread, so it hands work to the
    asyncio loop via call_soon_threadsafe instead of touching BLE directly.
    """

    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop,
                 queue: "asyncio.Queue[tuple[str, dict[str, list[bytes]]]]"):
        self._cfg = cfg
        self._loop = loop
        self._queue = queue

    def on_current_program_scene_changed(self, data) -> None:
        # obsws-python derives the OBS event from this method's name
        # (on_<event_snake_case>), so it must stay exactly this name.
        scene = data.scene_name
        logging.info("Scene changed → %s", scene)
        cmds = commands_for_scene(self._cfg, scene)
        if not cmds:
            logging.info("No profiles defined for '%s', skipping.", scene)
            return
        # Thread-safe hand-off into the asyncio loop.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (scene, cmds))


async def _command_worker(
    lights: dict[str, NeewerLight],
    queue: "asyncio.Queue[tuple[str, dict[str, list[bytes]]]]",
) -> None:
    """Serialise scene applications; send to each light concurrently.

    Coalesces bursts of scene changes to the most recent one (latest wins).
    """
    while True:
        scene, cmds = await queue.get()
        # Drain any backlog so rapid scene flips only apply the final state.
        while not queue.empty():
            scene, cmds = queue.get_nowait()
        await asyncio.gather(*(
            deliver(lights[name], packets, scene)
            for name, packets in cmds.items()
            if name in lights
        ))


def warn_unconfigured_scenes(cfg: Config) -> None:
    """Warn about OBS scenes that have no profile in the config."""
    try:
        client = obs.ReqClient(
            host=cfg.obs_host, port=cfg.obs_port, password=cfg.obs_password
        )
        resp = client.get_scene_list()
    except Exception as e:
        logging.warning("Could not verify OBS scenes (%s).", e)
        return

    names = [s["sceneName"] for s in resp.scenes]
    missing = [n for n in names if n not in cfg.scenes]
    if missing:
        logging.warning(
            "%d OBS scene(s) have no profile in config and will be ignored: %s",
            len(missing), ", ".join(missing),
        )
    else:
        logging.info("All %d OBS scene(s) have a profile in config.", len(names))

    try:
        client.disconnect()
    except Exception:
        pass


async def run_bridge(cfg: Config) -> None:
    if not cfg.lights:
        logging.error("No [lights] defined in config; add your lights first.")
        sys.exit(1)
    if not cfg.scenes:
        logging.error("No scene profiles defined. "
                      "Run with --init-scenes to scaffold them from OBS.")
        sys.exit(1)

    lights = {
        name: NeewerLight(name, address, cfg.char_uuid)
        for name, address in cfg.lights.items()
    }

    # Try to connect eagerly, but don't block startup if a light is off —
    # deliver() reconnects with backoff on the first command.
    for light in lights.values():
        await connect_with_backoff(light, max_attempts=3)

    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[tuple[str, dict[str, list[bytes]]]]" = asyncio.Queue()
    worker = asyncio.create_task(_command_worker(lights, queue))

    control = start_control_server(cfg, loop, queue) if cfg.control_enabled else None

    bridge = Bridge(cfg, loop, queue)
    try:
        event_client = obs.EventClient(
            host=cfg.obs_host, port=cfg.obs_port, password=cfg.obs_password
        )
    except Exception as e:
        logging.error(
            "Could not connect to OBS at %s:%d (%s). "
            "Is OBS running with WebSocket enabled and the password correct?",
            cfg.obs_host, cfg.obs_port, e,
        )
        worker.cancel()
        await asyncio.gather(*(light.disconnect() for light in lights.values()))
        sys.exit(1)

    event_client.callback.register(bridge.on_current_program_scene_changed)
    logging.info("Connected to OBS at %s:%d — managing %d light(s): %s",
                 cfg.obs_host, cfg.obs_port, len(lights), ", ".join(lights))
    warn_unconfigured_scenes(cfg)

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("Shutting down.")
    finally:
        worker.cancel()
        if control is not None:
            control.shutdown()
        try:
            event_client.disconnect()
        except Exception:
            pass
        await asyncio.gather(*(light.disconnect() for light in lights.values()))

# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OBS Scene → Neewer 660 Pro RGB bridge (multi-light)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  neewer-obs-bridge                      # start bridge
  neewer-obs-bridge --discover           # list BLE devices and UUIDs
  neewer-obs-bridge --list-scenes        # print OBS scene names
  neewer-obs-bridge --test-scene Gaming  # test one scene's profiles
""",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config TOML (default: {DEFAULT_CONFIG_PATH})",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--discover",
        action="store_true",
        help="Scan BLE, list all devices, dump Neewer services and UUIDs",
    )
    group.add_argument(
        "--list-scenes",
        action="store_true",
        help="Connect to OBS and print its scene names",
    )
    group.add_argument(
        "--init-scenes",
        action="store_true",
        help="Read OBS scenes and append default per-light profiles for missing ones",
    )
    group.add_argument(
        "--test-scene",
        metavar="SCENE_NAME",
        help="Send the per-light profiles for SCENE_NAME without OBS running",
    )
    group.add_argument(
        "--test-light",
        metavar="LIGHT_NAME",
        help="Send a bright 5600K test to ONE named light, to isolate it",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    # obsws logs a full traceback on connect failure even when we handle it;
    # keep our own clean message instead.
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
    args = parse_args()
    cfg = load_config(args.config)

    if args.discover:
        asyncio.run(run_discover(cfg))
    elif args.list_scenes:
        run_list_scenes(cfg)
    elif args.init_scenes:
        run_init_scenes(cfg, args.config)
    elif args.test_scene:
        asyncio.run(run_test_scene(cfg, args.test_scene))
    elif args.test_light:
        asyncio.run(run_test_light(cfg, args.test_light))
    else:
        asyncio.run(run_bridge(cfg))


if __name__ == "__main__":
    main()
