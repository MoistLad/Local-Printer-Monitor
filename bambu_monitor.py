"""
╔══════════════════════════════════════════╗
║   Bambu A1 + AMS Lite — System Monitor  ║
║   Real-time terminal dashboard via MQTT  ║
╚══════════════════════════════════════════╝

Usage:
  1. pip install -r requirements.txt
  2. python bambu_monitor.py
"""

import base64
import json
import ssl
import threading
import time

import paho.mqtt.client as mqtt
from bambulab import BambuAuthenticator, BambuAuthError, BambuClient
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

import config

# ── Everforest colour palette ─────────────────────────────────────────────────
T = {
    "fg":      "#d3c6aa",   # warm white  — normal text
    "dim":     "#475258",   # dim grey    — inactive / placeholders
    "accent":  "#7fbbb3",   # teal        — header, active, progress (low)
    "green":   "#a7c080",   # green       — online, OK, progress (high)
    "teal":    "#83c092",   # bright teal — AMS panel
    "yellow":  "#dbbc7f",   # amber       — temperatures, warnings
    "red":     "#e67e80",   # red         — errors, offline
    "purple":  "#d699b6",   # purple      — fans & speed
    "orange":  "#dbbc7f",   # amber alias — progress (mid)
}

# ── Cloud MQTT brokers (region-based) ────────────────────────────────────────
CLOUD_BROKERS = {
    "global": "us.mqtt.bambulab.com",
    "china":  "cn.mqtt.bambulab.com",
}
CLOUD_PORT = 8883

# ── Lookup tables ─────────────────────────────────────────────────────────────
SPEED_LABELS = {
    1: "Silent",
    2: "Standard",
    3: "Sport",
    4: "Ludicrous",
}

GCODE_STATE_META = {
    "IDLE":    ("IDLE",        T["dim"]),
    "RUNNING": ("▶  RUNNING",  f"bold {T['green']}"),
    "PAUSE":   ("⏸  PAUSED",   f"bold {T['yellow']}"),
    "FAILED":  ("✖  FAILED",   f"bold {T['red']}"),
    "FINISH":  ("✔  FINISHED", f"bold {T['accent']}"),
}

PRINT_TYPE_LABELS = {
    "idle":       "Idle",
    "cloud_file": "Cloud File",
    "local":      "Local File",
}


# ── Shared state ─────────────────────────────────────────────────────────────
state: dict = {
    "connected":    False,
    "connect_time": 0.0,   # timestamp of last successful connect
    "last_update":  None,
    "printer_name": "",
    "print":        {},
}
state_lock = threading.Lock()

# Grace period (seconds) before declaring OFFLINE after a disconnect event.
# Eliminates the brief flicker during paho's internal reconnect handshake.
_CONN_GRACE = 4.0


# ── Credential management ─────────────────────────────────────────────────────

def load_credentials() -> dict | None:
    try:
        with open(config.CREDENTIALS_FILE) as f:
            data = json.load(f)
        if all(k in data for k in ("email", "password", "serial", "region")):
            return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def save_credentials(creds: dict) -> None:
    with open(config.CREDENTIALS_FILE, "w") as f:
        json.dump(creds, f, indent=2)


def get_credentials(console: Console) -> dict:
    saved = load_credentials()
    if saved:
        console.print(
            f"[{T['green']}]✔  Loaded saved credentials[/]  "
            f"[{T['dim']}]({saved['email']}  /  serial: {saved['serial']})[/]\n"
            f"[{T['dim']}]Delete credentials.json to re-enter.[/]\n"
        )
        return saved

    console.print(f"[bold {T['accent']}]First-time setup[/] — enter your Bambu Lab details:\n")
    email  = Prompt.ask(f"  [{T['accent']}]Bambu account email[/]")
    passwd = Prompt.ask(f"  [{T['accent']}]Password[/]", password=True)
    serial = Prompt.ask(f"  [{T['accent']}]Printer serial number[/]  [{T['dim']}](Settings > Device)[/]")
    region = Prompt.ask(
        f"  [{T['accent']}]Region[/]",
        choices=["global", "china"],
        default="global",
    )

    creds = {
        "email":    email.strip(),
        "password": passwd,
        "serial":   serial.strip().upper(),
        "region":   region,
    }

    console.print()
    if Confirm.ask(f"  [{T['yellow']}]Save credentials locally to credentials.json?[/]", default=True):
        save_credentials(creds)
        console.print(f"[{T['green']}]✔  Saved[/]  [{T['dim']}](delete credentials.json to reset)[/]\n")
    else:
        console.print(f"[{T['dim']}]Credentials not saved — you'll be asked again next run.[/]\n")

    return creds


# ── Small helpers ─────────────────────────────────────────────────────────────

def fan_pct(raw: str) -> int:
    try:
        return round(int(raw) / 15 * 100)
    except (ValueError, TypeError):
        return 0


def fan_bar(raw: str) -> Text:
    pct    = fan_pct(raw)
    blocks = round(pct / 20)
    bar    = "█" * blocks + "░" * (5 - blocks)
    colour = T["green"] if pct < 60 else (T["yellow"] if pct < 85 else T["red"])
    return Text(f"{bar}  {pct:3d}%", style=colour)


def temp_style(current: float, target: float) -> str:
    if target == 0:
        return T["dim"]
    diff = current - target
    if abs(diff) <= 2:
        return T["green"]
    elif diff < 0:
        return T["yellow"]
    else:
        return T["red"]


def hex_to_rgb(hex_str: str):
    try:
        h = hex_str.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return None


def colour_swatch(hex_str: str, glyph: str = "██") -> Text:
    rgb = hex_to_rgb(hex_str)
    if rgb:
        return Text(glyph, style=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})")
    return Text("──", style=T["dim"])


def remain_bar(raw_val) -> Text:
    """
    raw_val = None  → field missing / unreadable → show N/A
    raw_val = 0     → confirmed empty spool      → red empty bar
    raw_val > 0     → normal coloured bar
    """
    if raw_val is None:
        return Text("N/A", style=T["dim"])
    try:
        pct = int(raw_val)
    except (TypeError, ValueError):
        return Text("N/A", style=T["dim"])

    filled = round(pct / 100 * 6)
    bar    = "█" * filled + "░" * (6 - filled)
    colour = T["green"] if pct > 50 else (T["yellow"] if pct > 20 else T["red"])
    return Text(f"{bar}  {pct:3d}%", style=colour)


def fmt_time(minutes: int) -> str:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "--:--"
    if m <= 0:
        return "--:--"
    h, rem = divmod(m, 60)
    return f"{h}h {rem:02d}m" if h else f"{rem}m"


# ── Panel builders ─────────────────────────────────────────────────────────────

def panel_header(p: dict) -> Panel:
    gcode = p.get("gcode_state", "IDLE").upper()
    state_label, state_style = GCODE_STATE_META.get(gcode, (gcode, T["fg"]))

    wifi     = p.get("wifi_signal", "N/A")
    nozzle_d = p.get("nozzle_diameter", "?")
    nozzle_t = p.get("nozzle_type", "").replace("_", " ").title()
    job_name = p.get("subtask_name", "—")
    ptype    = PRINT_TYPE_LABELS.get(p.get("print_type", "idle"), "—")

    with state_lock:
        raw_connected = state["connected"]
        connect_time  = state["connect_time"]
        last_updated  = state["last_update"] or "—"
        pname         = state["printer_name"] or ""

    # Grace period: suppress OFFLINE flicker for _CONN_GRACE seconds
    # after a successful connect (covers paho's internal reconnect handshake)
    connected = raw_connected or (time.time() - connect_time < _CONN_GRACE)

    now       = time.strftime("%H:%M:%S")
    conn_dot  = f"[bold {T['green']}]●[/]"      if connected else f"[bold {T['red']}]●[/]"
    conn_word = f"[bold {T['green']}]ONLINE[/]"  if connected else f"[bold {T['red']}]OFFLINE[/]"
    printer_label = f"  [{T['dim']}]|[/]  [bold {T['fg']}]{pname}[/]" if pname else ""

    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=4)
    grid.add_column(ratio=4)
    grid.add_column(ratio=2)

    grid.add_row(
        f"{conn_dot} {conn_word}{printer_label}",
        f"[{T['dim']}]Status:[/]  [{state_style}]{state_label}[/]",
        f"[{T['dim']}]📶[/]  [{T['fg']}]{wifi}[/]",
    )
    grid.add_row(
        f"[{T['dim']}]Job:[/]  [bold {T['fg']}]{job_name}[/]  [{T['dim']}]({ptype})[/]",
        f"[{T['dim']}]Nozzle:[/]  [bold {T['fg']}]{nozzle_d} mm  {nozzle_t}[/]",
        f"[{T['dim']}]Updated:[/]  [{T['fg']}]{last_updated}[/]",
    )
    return Panel(
        grid,
        title=f"[bold {T['accent']}] 🖨  BAMBU A1  MONITOR [/]",
        subtitle=f"[bold {T['fg']}] {now} [/]",
        subtitle_align="right",
        box=box.DOUBLE_EDGE,
        style=T["accent"],
        padding=(0, 1),
    )


def panel_temps(p: dict) -> Panel:
    nt  = float(p.get("nozzle_temper",        0))
    ntt = float(p.get("nozzle_target_temper", 0))
    bt  = float(p.get("bed_temper",           0))
    btt = float(p.get("bed_target_temper",    0))

    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(width=10)
    tbl.add_column(width=10)
    tbl.add_column(width=9)

    tbl.add_row(
        Text("Nozzle", style=f"bold {T['fg']}"),
        Text(f"{nt:.1f} °C", style=f"bold {temp_style(nt, ntt)}"),
        Text(f"↗ {ntt:.0f} °C", style=T["dim"]),
    )
    tbl.add_row(Text(""), Text(""), Text(""))
    tbl.add_row(
        Text("Bed", style=f"bold {T['fg']}"),
        Text(f"{bt:.1f} °C", style=f"bold {temp_style(bt, btt)}"),
        Text(f"↗ {btt:.0f} °C", style=T["dim"]),
    )
    return Panel(
        tbl,
        title=f"[bold {T['yellow']}]🌡  TEMPERATURES[/]",
        box=box.ROUNDED,
        border_style=T["yellow"],
        padding=(1, 2),
    )


def panel_progress(p: dict) -> Panel:
    pct   = int(p.get("mc_percent",        0))
    layer = int(p.get("layer_num",         0))
    total = int(p.get("total_layer_num",   0))
    rem   = int(p.get("mc_remaining_time", 0))

    bar_width = 32
    filled    = round(pct / 100 * bar_width)
    colour    = T["accent"] if pct < 40 else (T["yellow"] if pct < 75 else T["green"])
    bar_str   = "█" * filled + "░" * (bar_width - filled)

    bar_text = Text(bar_str, style=colour)
    bar_text.append(f"  {pct:3d}%", style=f"bold {T['fg']}")

    layer_str = f"Layer  {layer} / {total}" if total else f"Layer  {layer}"

    tbl = Table.grid(padding=(0, 0))
    tbl.add_column()
    tbl.add_row(bar_text)
    tbl.add_row(Text(""))
    tbl.add_row(Text(layer_str, style=f"bold {T['fg']}"))
    tbl.add_row(Text(f"⏱  {fmt_time(rem)}  remaining", style=T["dim"]))

    return Panel(
        tbl,
        title=f"[bold {T['accent']}]📊  PRINT PROGRESS[/]",
        box=box.ROUNDED,
        border_style=T["accent"],
        padding=(1, 2),
    )


def panel_fans_speed(p: dict) -> Panel:
    spd_mag   = p.get("spd_mag", 100)
    spd_lvl   = p.get("spd_lvl", 2)
    spd_label = SPEED_LABELS.get(int(spd_lvl), str(spd_lvl))
    cooling   = str(p.get("cooling_fan_speed",  "0"))
    heatbreak = str(p.get("heatbreak_fan_speed", "0"))

    spd_colour = (
        T["green"]  if int(spd_mag) <= 100 else
        T["yellow"] if int(spd_mag) <= 150 else
        T["red"]
    )

    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(width=12)
    tbl.add_column()

    tbl.add_row(
        Text("Speed", style=f"bold {T['fg']}"),
        Text(f"{spd_mag}%  [{spd_label}]", style=f"bold {spd_colour}"),
    )
    tbl.add_row(Text(""), Text(""))
    tbl.add_row(Text("Part Fan",   style=f"bold {T['fg']}"), fan_bar(cooling))
    tbl.add_row(Text("Hotend Fan", style=f"bold {T['fg']}"), fan_bar(heatbreak))

    return Panel(
        tbl,
        title=f"[bold {T['purple']}]⚙  SPEED & FANS[/]",
        box=box.ROUNDED,
        border_style=T["purple"],
        padding=(1, 2),
    )


def panel_ams(p: dict) -> Panel:
    ams_data = p.get("ams", {})
    ams_list = ams_data.get("ams", [])
    tray_now = str(ams_data.get("tray_now", "255"))
    vt       = p.get("vt_tray", {})

    tbl = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style=f"bold {T['dim']}",
        expand=True,
        padding=(0, 1),
    )
    tbl.add_column("Slot",   justify="center", width=6,  style=T["fg"])
    tbl.add_column("",       justify="center", width=3)
    tbl.add_column("Type",                     width=9,  style=f"bold {T['fg']}")
    tbl.add_column("Remain", justify="right",  width=14)

    if ams_list:
        unit  = ams_list[0]
        trays = unit.get("tray", [])

        for i in range(4):
            tray      = next((t for t in trays if str(t.get("id")) == str(i)), None)
            is_active = (str(i) == tray_now)
            marker    = " ◀" if is_active else ""
            slot_lbl  = Text(f" {i + 1}{marker}", style=f"bold {T['green']}" if is_active else T["dim"])

            if tray:
                col_hex  = tray.get("tray_color", "888888FF")
                # Use None sentinel if "remain" key is missing
                raw_rem  = tray.get("remain", None)
                tbl.add_row(
                    slot_lbl,
                    colour_swatch(col_hex),
                    Text(tray.get("tray_type", "?"), style=f"bold {T['fg']}"),
                    remain_bar(raw_rem),
                )
            else:
                tbl.add_row(
                    slot_lbl,
                    Text("──", style=T["dim"]),
                    Text("Empty", style=T["dim"]),
                    Text("──",   style=T["dim"]),
                )
    else:
        tbl.add_row(
            Text("N/A", style=T["dim"]),
            Text(""),
            Text("No AMS detected", style=T["dim"]),
            Text(""),
        )

    # ── External spool — always shown ────────────────────────────────────────
    is_ext_active = (tray_now == "254")
    ext_marker    = " ◀" if is_ext_active else ""
    ext_lbl       = Text(f" Ext{ext_marker}", style=f"bold {T['green']}" if is_ext_active else T["dim"])

    if vt and vt.get("tray_type"):
        col_hex = vt.get("tray_color", "888888FF")
        raw_rem = vt.get("remain", None)
        tbl.add_row(
            ext_lbl,
            colour_swatch(col_hex),
            Text(vt.get("tray_type", "?"), style=f"bold {T['fg']}"),
            remain_bar(raw_rem),
        )
    else:
        tbl.add_row(
            ext_lbl,
            Text("──", style=T["dim"]),
            Text("Empty", style=T["dim"]),
            Text("──",   style=T["dim"]),
        )

    return Panel(
        tbl,
        title=f"[bold {T['teal']}]🧵  AMS LITE  &  EXTERNAL SPOOL[/]",
        box=box.ROUNDED,
        border_style=T["teal"],
        padding=(0, 0),
    )


def panel_alerts(p: dict) -> Panel:
    hms  = p.get("hms", [])
    perr = p.get("print_error", 0)

    tbl = Table.grid(padding=(0, 2))
    tbl.add_column()

    if not hms and not perr:
        tbl.add_row(Text(f"✔  No active alerts", style=f"bold {T['green']}"))
    else:
        if perr:
            tbl.add_row(Text(f"⚠  Print Error: {hex(perr)}", style=f"bold {T['red']}"))
        for entry in hms:
            attr = entry.get("attr", 0)
            code = entry.get("code", 0)
            tbl.add_row(Text(f"⚠  HMS  attr={hex(attr)}  code={hex(code)}", style=f"bold {T['yellow']}"))

    return Panel(
        tbl,
        title=f"[bold {T['red']}]🚨  ALERTS[/]",
        box=box.ROUNDED,
        border_style=T["red"],
        padding=(0, 2),
    )


# ── Dashboard compositor ──────────────────────────────────────────────────────

def build_dashboard() -> Layout:
    with state_lock:
        p = dict(state["print"])

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=6),   # fixed: title bar
        Layout(name="middle", ratio=2),  # flexible: temps / progress / fans
        Layout(name="ams",    ratio=3),  # flexible: AMS
        Layout(name="alerts", size=5),   # fixed: alert bar
    )
    layout["middle"].split_row(
        Layout(name="temps",    ratio=1),
        Layout(name="progress", ratio=2),
        Layout(name="fans",     ratio=1),
    )

    layout["header"].update(panel_header(p))
    layout["temps"].update(panel_temps(p))
    layout["progress"].update(panel_progress(p))
    layout["fans"].update(panel_fans_speed(p))
    layout["ams"].update(panel_ams(p))
    layout["alerts"].update(panel_alerts(p))

    return layout


# ── MQTT callbacks ────────────────────────────────────────────────────────────

def on_connect(client, userdata, connect_flags, reason_code, properties):
    serial = userdata["serial"]
    if reason_code.is_failure:
        with state_lock:
            state["connected"] = False
    else:
        client.subscribe(f"device/{serial}/report")
        with state_lock:
            state["connected"]    = True
            state["connect_time"] = time.time()   # record successful connect
        pushall = json.dumps({"pushing": {"command": "pushall", "version": 1}})
        client.publish(f"device/{serial}/request", pushall)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    with state_lock:
        state["connected"] = False
        # connect_time is intentionally NOT reset here so the grace period can
        # suppress brief flicker caused by paho's internal reconnect handshake.


def on_message(client, userdata, msg):
    try:
        payload    = json.loads(msg.payload.decode("utf-8"))
        print_data = payload.get("print", {})
        if print_data:
            with state_lock:
                state["print"].update(print_data)
                state["last_update"] = time.strftime("%H:%M:%S")
    except Exception:
        pass


# ── MQTT client setup ─────────────────────────────────────────────────────────

def _connect_with_retry(client: mqtt.Client, broker: str) -> None:
    while True:
        try:
            client.connect(broker, CLOUD_PORT, keepalive=60)
            return
        except Exception:
            with state_lock:
                state["connected"] = False
            time.sleep(10)


def start_mqtt(uid: str, token: str, serial: str, region: str) -> mqtt.Client:
    broker = CLOUD_BROKERS.get(region, CLOUD_BROKERS["global"])
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bambu-monitor-{serial}",
    )
    client.username_pw_set(uid, token)
    client.user_data_set({"serial": serial})
    tls_ctx = ssl.create_default_context()
    client.tls_set_context(tls_ctx)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.loop_start()
    threading.Thread(
        target=_connect_with_retry, args=(client, broker), daemon=True
    ).start()
    return client


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    console = Console()
    console.print(
        f"\n[bold {T['accent']}]🖨  Bambu A1 Monitor[/]  —  Bambu Cloud MQTT\n"
        f"[{T['dim']}]Press  Ctrl+C  to exit[/]\n"
    )

    # ── Step 1: Credentials ───────────────────────────────────────────────────
    creds = get_credentials(console)

    # ── Step 2: Authenticate ──────────────────────────────────────────────────
    console.print(f"[{T['dim']}]Authenticating with Bambu Lab cloud...[/]")
    auth = BambuAuthenticator(region=creds["region"])
    try:
        token = auth.get_or_create_token(
            username=creds["email"],
            password=creds["password"],
        )
        console.print(f"[bold {T['green']}]✔  Authenticated[/]\n")
    except BambuAuthError as exc:
        console.print(f"[bold {T['red']}]✖  Authentication failed:[/]  {exc}\n")
        console.print(f"[{T['dim']}]Delete credentials.json and re-run to enter new credentials.[/]\n")
        return

    # ── Step 3: Resolve MQTT username ─────────────────────────────────────────
    uid = creds.get("mqtt_username", "").strip()
    if not uid or not uid.startswith("u_"):
        console.print(f"[{T['dim']}]Fetching MQTT username from Bambu API...[/]")
        try:
            api = BambuClient(token=token)
            info = api.get_user_info()
            numeric_uid = info.get("uid", "")
            uid = f"u_{numeric_uid}" if numeric_uid else "u_unknown"
            creds["mqtt_username"] = uid
            save_credentials(creds)
            console.print(f"[bold {T['green']}]✔  MQTT username: {uid}[/]\n")
        except Exception as e:
            console.print(f"[bold {T['red']}]Could not fetch user info: {e}[/]\n")
            uid = "u_unknown"

    # ── Step 4: Fetch printer name ────────────────────────────────────────────
    console.print(f"[{T['dim']}]Fetching printer info...[/]")
    try:
        api      = BambuClient(token=token)
        devices  = api.get_devices()
        # get_devices returns a list of device dicts
        if isinstance(devices, dict):
            devices = devices.get("devices", devices.get("data", []))
        dev = next(
            (d for d in (devices or []) if str(d.get("dev_id", d.get("serial", ""))) == creds["serial"]),
            None,
        )
        pname = (dev or {}).get("name", (dev or {}).get("dev_name", ""))
        with state_lock:
            state["printer_name"] = pname
        if pname:
            console.print(f"[bold {T['green']}]✔  Printer: {pname}[/]\n")
    except Exception as e:
        console.print(f"[{T['dim']}]Could not fetch printer name: {e}[/]\n")

    # ── Step 5: Start MQTT ────────────────────────────────────────────────────
    broker = CLOUD_BROKERS.get(creds["region"], CLOUD_BROKERS["global"])
    console.print(f"[{T['dim']}]Connecting to {broker}:{CLOUD_PORT}  (serial: {creds['serial']})[/]")
    client = start_mqtt(uid, token, creds["serial"], creds["region"])

    # ── Step 6: Full-screen live dashboard ────────────────────────────────────
    try:
        with Live(
            build_dashboard(),
            console=console,
            refresh_per_second=int(1 / config.REFRESH_RATE),
            screen=True,
        ) as live:
            while True:
                live.update(build_dashboard())
                time.sleep(config.REFRESH_RATE)

    except KeyboardInterrupt:
        pass

    finally:
        client.loop_stop()
        client.disconnect()
        console.print(f"\n[bold {T['accent']}]Monitor stopped.[/]\n")


if __name__ == "__main__":
    main()
