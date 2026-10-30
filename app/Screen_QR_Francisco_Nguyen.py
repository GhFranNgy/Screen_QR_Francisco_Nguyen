"""
Francisco Nguyen's Screen QR Sharer
===================================
Created by: Francisco Nguyen - Claude Sonnet 5.5

Run this on your laptop. It shows a QR code. On your ANDROID phone or tablet
(Android 11+, same Wi-Fi) open:

    Settings > Developer options > Wireless debugging > Pair device with QR code

and scan the code. The script pairs with the device, connects, and opens a
window on your laptop that mirrors (and lets you control) its screen.

Every action is printed in color to the console (and saved, without colors,
to phone_mirror.log) from startup until you close the program.

Install:
    pip install zeroconf "qrcode[pil]"
    + scrcpy and adb (see below)
        Windows : download scrcpy from github.com/Genymobile/scrcpy/releases
                  (the zip includes adb) and add its folder to PATH
        macOS   : brew install scrcpy android-platform-tools
        Linux   : sudo apt install scrcpy adb

Run:   python phone_mirror_qr.py
Stop:  close the mirror window, or press Ctrl+C
"""

import atexit
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

import qrcode
from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

WATERMARK = "Francisco Nguyen - Claude Sonnet 5.5"
APP_NAME = "Francisco Nguyen's Screen QR Sharer"

# ---- Settings -------------------------------------------------------------
PAIR_TIMEOUT = 180       # seconds to wait for you to scan the QR code
CONNECT_TIMEOUT = 60     # seconds to wait for the device to become connectable
LOG_FILE = "phone_mirror.log"
QR_FILE = "phone_mirror_qr.png"
# ---------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "phone_mirror_settings.json"
)

# What you can pick in the menu (label, value). Values go to scrcpy.
RESOLUTIONS = [
    ("Tiny      - 640 px", 640),
    ("Small     - 960 px", 960),
    ("Medium    - 1280 px", 1280),
    ("Large     - 1600 px", 1600),
    ("Full HD   - 1920 px", 1920),
    ("Native    - original resolution", 0),
]
FPS_OPTIONS = [
    ("30 fps", 30),
    ("60 fps", 60),
    ("90 fps", 90),
    ("120 fps", 120),
    ("Device maximum (no cap)", 0),
]
BITRATES = [
    ("4 Mbps   - low bandwidth", "4M"),
    ("8 Mbps   - normal", "8M"),
    ("16 Mbps  - smooth", "16M"),
    ("32 Mbps  - very high quality", "32M"),
    ("64 Mbps  - maximum (needs strong Wi-Fi)", "64M"),
]
PRESETS = {
    "Fast & light": {"max_size": 960,  "max_fps": 30, "bitrate": "4M"},
    "Balanced":     {"max_size": 1280, "max_fps": 60, "bitrate": "8M"},
    "Smooth":       {"max_size": 1280, "max_fps": 0,  "bitrate": "16M"},
    "Max quality":  {"max_size": 0,    "max_fps": 0,  "bitrate": "32M"},
}
DEFAULT_SETTINGS = {
    "max_size": 1280, "max_fps": 0, "bitrate": "16M",
    "stay_awake": False, "screen_off": False, "audio": True,
}

PAIR_SERVICE = "_adb-tls-pairing._tcp.local."
CONNECT_SERVICE = "_adb-tls-connect._tcp.local."

# ---- Colors ---------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8")  # so logo/QR characters always print
except Exception:
    pass
if os.name == "nt":
    os.system("")  # switches on ANSI color support in Windows 10/11 consoles

COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
RESET = "\033[0m" if COLOR else ""
BOLD = "\033[1m" if COLOR else ""
DIM = "\033[2m" if COLOR else ""


def fg(n):
    """256-colour foreground code."""
    return f"\033[38;5;{n}m" if COLOR else ""


def paint(text, *codes):
    return "".join(codes) + text + RESET if COLOR else text


def gradient(text, palette):
    """Colour a string with a left-to-right gradient."""
    if not COLOR:
        return text
    out, n = [], max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        out.append(fg(palette[i * (len(palette) - 1) // n]) + ch)
    return BOLD + "".join(out) + RESET


GRADIENT = [51, 45, 39, 33, 63, 99, 135, 171, 207]   # cyan -> magenta

# ---- Logging (coloured console + plain file) ------------------------------
OK_LEVEL = 25
logging.addLevelName(OK_LEVEL, "OK")

LEVEL_STYLE = {
    logging.DEBUG:   (fg(244), fg(244)),
    logging.INFO:    (fg(81),  fg(255)),
    OK_LEVEL:        (BOLD + fg(46), fg(120)),
    logging.WARNING: (BOLD + fg(214), fg(221)),
    logging.ERROR:   (BOLD + fg(196), fg(203)),
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        ts = self.formatTime(record, "%H:%M:%S")
        tag_c, msg_c = LEVEL_STYLE.get(record.levelno, ("", ""))
        return (
            f"{DIM}{ts}{RESET} {fg(240)}|{RESET} "
            f"{tag_c}{record.levelname:<7}{RESET} {fg(240)}|{RESET} "
            f"{msg_c}{record.getMessage()}{RESET}"
        )


log = logging.getLogger("phone_mirror")
log.setLevel(logging.DEBUG)
_console = logging.StreamHandler()
_console.setFormatter(ColorFormatter())
log.addHandler(_console)
try:
    _file = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _file.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    )
    log.addHandler(_file)
except OSError:
    pass


def ok(msg):
    log.log(OK_LEVEL, msg)


START_TIME = time.time()
state = {"device": None, "paired": False, "connected": False, "mirror_secs": 0.0}


# ---- Logo -----------------------------------------------------------------
def banner():
    left = ["███████", "█     █", "█ ███ █", "█ ███ █", "█ ███ █", "█     █", "███████"]
    mid = [" █ █ ", " █   ", "   █ ", " ███ ", "  █  ", " █ █ ", "   █ "]
    icon = [f"{l}{m}{l}" for l, m in zip(left, mid)]

    text = [
        "",
        gradient("F R A N C I S C O   N G U Y E N ' S", GRADIENT),
        paint("─" * 35, fg(240)),
        gradient("S C R E E N   Q R   S H A R E R", GRADIENT[::-1]),
        "",
        paint("mirror your Android screen with one scan", DIM, fg(250)),
        paint(f"made by {WATERMARK}", DIM, fg(244)),
    ]

    width = 74
    print()
    print(paint("╔" + "═" * (width - 2) + "╗", fg(39)))
    for i, row in enumerate(icon):
        # colour the icon with a vertical gradient
        icon_col = paint(row, BOLD, fg(GRADIENT[i * (len(GRADIENT) - 1) // 6]))
        plain_len = 2 + len(row) + 4 + len(_strip(text[i]))
        pad = " " * max(width - 2 - plain_len, 0)
        print(
            paint("║", fg(39)) + "  " + icon_col + "    " + text[i] + pad
            + paint("║", fg(39))
        )
    print(paint("╚" + "═" * (width - 2) + "╝", fg(39)))
    print()


def _strip(s):
    """Length helper: remove ANSI codes so padding lines up."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


# ---- Helpers --------------------------------------------------------------
def run(cmd, timeout=30):
    """Run a command, log it and its output, return (returncode, output)."""
    log.info(f"Running: {' '.join(cmd)}")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        return 1, ""
    out = (p.stdout + p.stderr).strip()
    for line in out.splitlines():
        log.debug(f"  > {line}")
    return p.returncode, out


def discover(zc, service_type, matcher, timeout, label):
    """Wait until a service matching `matcher(name, ipv4_list)` shows up."""
    found = {}
    event = threading.Event()

    class Listener(ServiceListener):
        def _check(self, zc_, type_, name):
            if event.is_set():
                return
            log.debug(f"Saw {label} service: {name}")
            info = zc_.get_service_info(type_, name, timeout=3000)
            if not info:
                return
            addrs = info.parsed_addresses(IPVersion.V4Only)
            if addrs and matcher(name, addrs):
                found.update(ip=addrs[0], port=info.port, name=name)
                event.set()

        def add_service(self, zc_, type_, name):
            self._check(zc_, type_, name)

        def update_service(self, zc_, type_, name):
            self._check(zc_, type_, name)

        def remove_service(self, zc_, type_, name):
            pass

    browser = ServiceBrowser(zc, service_type, Listener())
    ok_ = event.wait(timeout)
    browser.cancel()
    return found if ok_ else None


# ---- Settings & menu ------------------------------------------------------
def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        s.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        log.info(f"Loaded saved settings from {os.path.basename(SETTINGS_FILE)}")
    except FileNotFoundError:
        log.info("No saved settings yet - using defaults")
    except (OSError, ValueError) as exc:
        log.warning(f"Could not read saved settings ({exc!r}) - using defaults")
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        log.info(f"Settings saved to {os.path.basename(SETTINGS_FILE)}")
    except OSError as exc:
        log.warning(f"Could not save settings: {exc!r}")


def label_for(options, value):
    for label, v in options:
        if v == value:
            return label
    return str(value)


def describe_settings(s):
    fps = f"{s['max_fps']} fps" if s["max_fps"] else "max fps"
    size = f"{s['max_size']} px" if s["max_size"] else "native resolution"
    return f"{size}, {fps}, {s['bitrate']}bit/s"


def build_scrcpy_args(s):
    args = ["--window-title", APP_NAME, "-b", s["bitrate"]]
    if s["max_size"]:
        args += ["--max-size", str(s["max_size"])]
    if s["max_fps"]:
        args += ["--max-fps", str(s["max_fps"])]
    if s["stay_awake"]:
        args.append("--stay-awake")
    if s["screen_off"]:
        args.append("--turn-screen-off")
    if not s["audio"]:
        args.append("--no-audio")
    return args


def pick(title, options, current):
    """Show a numbered list and return the chosen value."""
    print()
    print(paint(title, BOLD, fg(81)))
    for i, (label, value) in enumerate(options, 1):
        tag = paint("[" + str(i) + "]", fg(221))
        mark = paint("   <- current", fg(46)) if value == current else ""
        print(f"  {tag} {label}{mark}")
    while True:
        raw = input(paint("Choose a number (Enter = keep current): ", fg(255))).strip()
        if raw == "":
            return current
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print(paint("  Not a valid choice, try again.", fg(203)))


def show_menu(s):
    """Interactive settings menu. Returns True to start, False to quit."""
    log.info("Opening settings menu")
    while True:
        on = paint("ON", BOLD, fg(46))
        off = paint("OFF", fg(244))
        rows = [
            ("1", "Resolution (longest side)", label_for(RESOLUTIONS, s["max_size"])),
            ("2", "Frame rate (fps)", label_for(FPS_OPTIONS, s["max_fps"])),
            ("3", "Bitrate (quality)", label_for(BITRATES, s["bitrate"])),
            ("4", "Presets", "Fast / Balanced / Smooth / Max quality"),
            ("5", "Keep device awake", on if s["stay_awake"] else off),
            ("6", "Turn device screen off", on if s["screen_off"] else off),
            ("7", "Sound from device", on if s["audio"] else off),
        ]
        print()
        print(paint("=" * 64, fg(39)))
        print(gradient("  S E T T I N G S", GRADIENT))
        print(paint("=" * 64, fg(39)))
        for key, name, value in rows:
            print(f"  {paint('[' + key + ']', fg(221))} {name:<27} {value}")
        print(f"  {paint('[S]', fg(46))} Start mirroring     {paint('[Q]', fg(203))} Quit")

        try:
            choice = input(paint("\nChoose (Enter = start): ", fg(255))).strip().lower()
        except EOFError:
            return True

        if choice in ("", "s"):
            return True
        if choice == "q":
            return False
        if choice == "1":
            s["max_size"] = pick("Resolution (px = longest side of the screen)", RESOLUTIONS, s["max_size"])
            log.info(f"Resolution set to: {label_for(RESOLUTIONS, s['max_size'])}")
        elif choice == "2":
            s["max_fps"] = pick("Frame rate cap", FPS_OPTIONS, s["max_fps"])
            log.info(f"Frame rate set to: {label_for(FPS_OPTIONS, s['max_fps'])}")
        elif choice == "3":
            s["bitrate"] = pick("Bitrate", BITRATES, s["bitrate"])
            log.info(f"Bitrate set to: {s['bitrate']}")
        elif choice == "4":
            opts = []
            for name, p in PRESETS.items():
                opts.append((f"{name:<13} ({describe_settings({**s, **p})})", name))
            name = pick("Presets", opts, None)
            if name:
                s.update(PRESETS[name])
                log.info(f"Preset applied: {name} ({describe_settings(s)})")
        elif choice == "5":
            s["stay_awake"] = not s["stay_awake"]
            log.info(f"Keep device awake: {'ON' if s['stay_awake'] else 'OFF'}")
        elif choice == "6":
            s["screen_off"] = not s["screen_off"]
            log.info(f"Turn device screen off: {'ON' if s['screen_off'] else 'OFF'}")
        elif choice == "7":
            s["audio"] = not s["audio"]
            log.info(f"Sound from device: {'ON' if s['audio'] else 'OFF'} "
                     "(needs scrcpy 2.0+ and Android 11+)")
        else:
            print(paint("  Not a valid choice.", fg(203)))


def shutdown_summary():
    up = time.time() - START_TIME
    line = paint("-" * 60, fg(240))
    log.info(line)
    log.info(paint("Session summary", BOLD, fg(81)))
    log.info(f"  Uptime:        {up:.1f}s")
    log.info(f"  Device:        {state['device'] or 'none'}")
    log.info(f"  Paired:        {state['paired']}")
    log.info(f"  Connected:     {state['connected']}")
    log.info(f"  Mirrored for:  {state['mirror_secs']:.1f}s")
    log.info(f"  Settings file: {os.path.basename(SETTINGS_FILE)}")
    log.info(f"  Log saved to:  {LOG_FILE}")
    log.info(f"{APP_NAME} closed. Created by {WATERMARK}.")
    log.info(line)


def _handle_signal(signum, _frame):
    log.info(f"Received signal {signum}, shutting down...")
    raise KeyboardInterrupt


def main():
    banner()
    log.info(f"{APP_NAME} starting")

    # 1. Check required tools
    log.info("Checking for required tools (adb, scrcpy)...")
    missing = [t for t in ("adb", "scrcpy") if not shutil.which(t)]
    if missing:
        log.error(f"Missing from PATH: {', '.join(missing)}")
        log.error("Install scrcpy + adb - see the instructions at the top of this file.")
        return
    ok("adb and scrcpy found")

    for sig_name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_signal)
            except (ValueError, OSError):
                pass
    atexit.register(shutdown_summary)

    zc = None
    target = None
    try:
        # Settings menu (skipped when there is no interactive terminal)
        settings = load_settings()
        if sys.stdin.isatty():
            if not show_menu(settings):
                log.info("Quit from menu")
                return
        else:
            log.info("Non-interactive terminal - skipping menu")
        save_settings(settings)
        log.info("Mirror settings: " + describe_settings(settings))

        # 2. Start adb
        log.info("Starting adb server...")
        run(["adb", "start-server"])

        # 3. Build the QR code (Android's wireless-debugging pairing format)
        instance = "ADB_WIFI_" + secrets.token_hex(3)
        password = secrets.token_hex(4)
        log.info(f"Generated one-time pairing name '{instance}' and password")
        payload = f"WIFI:T:ADB;S:{instance};P:{password};;"

        log.info("Generating QR code...")
        qr = qrcode.QRCode(border=2)
        qr.add_data(payload)
        qr.make(fit=True)
        print()
        print(paint("On your Android phone/tablet open:", BOLD, fg(255)))
        print(paint("  Settings > Developer options > Wireless debugging", fg(221)))
        print(paint("  > Pair device with QR code", fg(221)) + paint("   (then scan this code)", fg(250)))
        print()
        qr.print_ascii(invert=True)
        print()
        try:
            qr.make_image().save(QR_FILE)
            ok(f"QR code image saved as {QR_FILE}")
        except Exception as exc:
            log.warning(f"Could not save {QR_FILE}: {exc!r}")

        # 4. Wait for the device to advertise the pairing service
        log.info("Starting network discovery (mDNS)...")
        zc = Zeroconf()
        log.info(f"Waiting up to {PAIR_TIMEOUT}s for the device to scan the QR code...")
        pair = discover(
            zc, PAIR_SERVICE,
            lambda name, addrs: name.startswith(instance + "."),
            PAIR_TIMEOUT, "pairing",
        )
        if not pair:
            log.error("Timed out - the QR code was not scanned. Run the script again.")
            return
        phone_ip = pair["ip"]
        state["device"] = phone_ip
        ok(f"Device found at {phone_ip}:{pair['port']}")

        # 5. Pair
        log.info("Pairing with device...")
        code, out = run(["adb", "pair", f"{phone_ip}:{pair['port']}", password])
        if "Successfully paired" not in out:
            log.error("Pairing failed. Check the device is on the same Wi-Fi and retry.")
            return
        state["paired"] = True
        ok("Paired successfully")

        # 6. Find the connect port (different from the pairing port)
        log.info(f"Waiting up to {CONNECT_TIMEOUT}s for the device's connect service...")
        conn = discover(
            zc, CONNECT_SERVICE,
            lambda name, addrs: phone_ip in addrs,
            CONNECT_TIMEOUT, "connect",
        )
        if not conn:
            log.error("Device did not advertise a connect port. Make sure "
                      "'Wireless debugging' is still switched on, then retry.")
            return
        target = f"{conn['ip']}:{conn['port']}"
        log.info(f"Connect address: {target}")

        # 7. Connect
        log.info("Connecting adb to device...")
        code, out = run(["adb", "connect", target])
        if "connected" not in out.lower():
            log.error("adb connect failed.")
            return
        state["connected"] = True
        ok("Connected")

        # 8. Mirror
        ok("Launching scrcpy - the mirror window should open now")
        log.info("Close the mirror window or press Ctrl+C to stop")
        began = time.time()
        args = build_scrcpy_args(settings)
        log.info(f"Running: scrcpy -s {target} {' '.join(args)}")
        proc = subprocess.Popen(["scrcpy", "-s", target] + args)
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            log.info("Stop requested (Ctrl+C) - closing mirror window")
            proc.terminate()
            proc.wait()
            raise
        finally:
            state["mirror_secs"] = time.time() - began
        log.info(f"Mirror window closed (scrcpy exit code {code})")

    except KeyboardInterrupt:
        log.info("Stop requested (Ctrl+C)")
    finally:
        if zc:
            zc.close()
            log.info("Network discovery stopped")
        if target:
            log.info("Disconnecting adb from device...")
            run(["adb", "disconnect", target])


if __name__ == "__main__":
    main()