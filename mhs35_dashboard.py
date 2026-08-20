#!/usr/bin/env python3
"""
MHS-3.5 SPI framebuffer stats dashboard — portrait, touch-enabled.

Home screen: big glanceable CPU/MEM/DISK numbers + a single Docker
status light (worst state across all containers). Tap it to open the
Docker detail screen (per-container name + status), tap "< BACK" to
return.

Install deps:
    sudo pip3 install psutil pillow numpy docker evdev --break-system-packages

Run manually to test:
    sudo python3 mhs35_dashboard.py
"""

import time
import socket
import threading
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import psutil
except ImportError:
    raise SystemExit("pip3 install psutil --break-system-packages")

try:
    import docker
    _docker_client = docker.from_env()
except Exception:
    _docker_client = None

try:
    import evdev
    from evdev import ecodes
except ImportError:
    evdev = None  # touch will just be disabled, dashboard still runs

# ---- CONFIG -------------------------------------------------------------
FB_DEVICE = "/dev/fb0"
WIDTH, HEIGHT = 320, 480   # portrait, panel rotated 90 degrees
ROTATE = 0                 # leave at 0 if the overlay/kernel already rotates
REFRESH_SECONDS = 2

DISK_EXCLUDE_MOUNTS = ("/boot/firmware",)
MAX_DISK_ROWS = 5

# Interface name prefixes used to classify links. Pi defaults: wlan0 / eth0.
WIFI_PREFIXES = ("wlan", "wl")
ETH_PREFIXES = ("eth", "en")
NET_CACHE_SECONDS = 5

# Touch device — leave None to auto-detect. Set explicitly (e.g.
# "/dev/input/event0") if auto-detect picks the wrong device.
TOUCH_DEVICE = None
# If taps land in the wrong spot, try flipping these before touching
# kernel-level calibration.
TOUCH_SWAP_XY = False
TOUCH_INVERT_X = False
TOUCH_INVERT_Y = False

BG = (10, 12, 16)
FG = (235, 235, 235)
DIM = (120, 125, 135)
ACCENT = (70, 160, 230)
OK_GREEN = (70, 200, 120)
BAD_RED = (225, 70, 70)
WARN_YELLOW = (230, 180, 60)

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
try:
    F_HUGE = ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 56)
    F_LG = ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 26)
    F_MD = ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", 20)
    F_SM = ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", 16)
except OSError:
    F_HUGE = ImageFont.load_default(56)
    F_LG = ImageFont.load_default(26)
    F_MD = ImageFont.load_default(20)
    F_SM = ImageFont.load_default(16)

RED_STATUSES = {"exited", "dead", "removing"}
YELLOW_STATUSES = {"restarting", "paused", "created"}

# ---- SHARED STATE ---------------------------------------------------------
state_lock = threading.Lock()
update_event = threading.Event()
current_page = "home"   # "home" | "docker"

# Button hit-regions as (x0, y0, x1, y1), defined once layout is fixed.
HOME_DOCKER_BUTTON = (16, 336, WIDTH - 16, 466)
DOCKER_BACK_BUTTON = (16, 16, 140, 60)

# ---- FRAMEBUFFER I/O ------------------------------------------------------

def image_to_rgb565_bytes(img: Image.Image) -> bytes:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (arr[:, :, 0] >> 3) << 11
    g = (arr[:, :, 1] >> 2) << 5
    b = (arr[:, :, 2] >> 3)
    return (r | g | b).astype(np.uint16).tobytes()


def push_frame(img: Image.Image):
    if ROTATE:
        img = img.rotate(ROTATE, expand=True)
    with open(FB_DEVICE, "wb") as fb:
        fb.write(image_to_rgb565_bytes(img))

# ---- STAT COLLECTION -------------------------------------------------------

_last_net = psutil.net_io_counters()
_last_net_time = time.time()


def get_net_rates():
    global _last_net, _last_net_time
    now = time.time()
    cur = psutil.net_io_counters()
    dt = max(now - _last_net_time, 0.001)
    up = (cur.bytes_sent - _last_net.bytes_sent) / dt
    down = (cur.bytes_recv - _last_net.bytes_recv) / dt
    _last_net, _last_net_time = cur, now
    return up, down

_net_cache = {"t": 0.0, "val": None}

def _link_state(stats, addrs, prefixes):
    """(color, present) for the healthiest interface matching prefixes.
    green = up with a real IPv4, yellow = up but no usable address,
    red = present but down, DIM = no such interface."""
    best = None
    for name, st in stats.items():
        if not name.startswith(prefixes):
            continue
        has_ip = any(a.family == socket.AF_INET
                     and not a.address.startswith("169.254.")
                     for a in addrs.get(name, ()))
        rank = 0 if (st.isup and has_ip) else 1 if st.isup else 2
        if best is None or rank < best:
            best = rank
    if best is None:
        return DIM, False
    return (OK_GREEN, WARN_YELLOW, BAD_RED)[best], True


def get_link_states():
    """((wifi_color, wifi_present), (eth_color, eth_present)), cached for a
    few seconds so it isn't re-read on every repaint."""
    now = time.time()
    if _net_cache["val"] is not None and now - _net_cache["t"] < NET_CACHE_SECONDS:
        return _net_cache["val"]
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        val = (_link_state(stats, addrs, WIFI_PREFIXES),
               _link_state(stats, addrs, ETH_PREFIXES))
    except OSError:
        val = ((DIM, False), (DIM, False))
    _net_cache.update(t=now, val=val)
    return val

def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"

def get_disks():
    """[(mountpoint, usage), ...] for real filesystems, skipping the
    mounts named in DISK_EXCLUDE_MOUNTS and any duplicate devices."""
    disks = []
    seen_devices = set()
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if any(mp == ex or mp.startswith(ex.rstrip("/") + "/")
            for ex in DISK_EXCLUDE_MOUNTS):
                continue
        if part.device in seen_devices:
            continue
        try:
            usage = psutil.disk_usage(mp)
        except (PermissionError, OSError):
            continue
        if usage.total == 0:
            continue
        seen_devices.add(part.device)
        disks.append((mp, usage))
        
    disks.sort(key=lambda d: (d[0] != "/", d[0]))  # "/" first, then alphabetical
    return disks

def get_containers():
    if _docker_client is None:
        return None
    try:
        return sorted((c.name, c.status) for c in _docker_client.containers.list(all=True))
    except Exception:
        return None


def worst_docker_status(containers):
    """Returns (color, label) for the worst state across all containers."""
    if containers is None:
        return BAD_RED, "ERROR"
    if not containers:
        return OK_GREEN, "NONE"
    worst = OK_GREEN
    for _, status in containers:
        #ignore any containers with this text because we don't care about them
        if name.find('ignore-status') >= 0:
            continue
        if status in RED_STATUSES:
            return BAD_RED, "ISSUE"
        if status in YELLOW_STATUSES:
            worst = WARN_YELLOW
    return (worst, "WARN" if worst == WARN_YELLOW else "OK")

# ---- DRAWING HELPERS --------------------------------------------------------

def fit_text(d, text, font, max_w, keep="left"):
    """Truncate with an ellipsis so text fits in max_w. keep='right'
    trims from the front, which reads better for long mount paths."""
    if d.textlength(text, font=font) <= max_w:
        return text
    ell = "\u2026"
    if keep == "right":
        while text and d.textlength(ell + text, font=font) > max_w:
            text = text[1:]
        return ell + text
    while text and d.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return text + ell


def draw_disk_row(d, x0, y0, w, h, mount, usage):
    """Compact one-line version of draw_stat_section: mount on the left,
    percentage and used/total on the right, bar as the background."""
    pct = usage.percent
    x1, y1 = x0 + w, y0 + h
    d.rectangle([x0, y0, x1, y1], outline=DIM, width=2)
    fill_w = int((w - 4) * min(max(pct, 0), 100) / 100)
    if fill_w > 0:
        d.rectangle([x0 + 2, y0 + 2, x0 + 2 + fill_w, y1 - 2], fill=pct_color(pct))

    font = F_MD if h >= 34 else F_SM
    right = f"{pct:.0f}%  {human_bytes(usage.used)}/{human_bytes(usage.total)}"
    rw = d.textlength(right, font=font)
    label = fit_text(d, mount, font, w - rw - 28, keep="right")

    bbox = d.textbbox((0, 0), right, font=font)
    ty = y0 + (h - (bbox[3] - bbox[1])) / 2 - bbox[1]
    draw_shadow_text(d, (x0 + 10, ty), label, font)
    draw_shadow_text(d, (x1 - 10 - rw, ty), right, font)

def draw_bar(draw, x, y, w, h, pct, color):
    draw.rectangle([x, y, x + w, y + h], outline=DIM, width=2)
    fill_w = int((w - 4) * min(max(pct, 0), 100) / 100)
    if fill_w > 0:
        draw.rectangle([x + 2, y + 2, x + 2 + fill_w, y + h - 2], fill=color)

def draw_shadow_text(d, xy, text, font, fill=FG):
    """Text with a 1px dark shadow so it stays readable over any bar fill color."""
    x, y = xy
    d.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0))
    d.text((x, y), text, font=font, fill=fill)

def draw_net_status(d, x0, x1, y):
    """Wifi/eth indicators centered in the span x0..x1. Tries full labels,
    then single letters, then bare dots — whatever fits the gap the clock
    and hostname leave behind."""
    (wifi_color, wifi_up), (eth_color, eth_up) = get_link_states()
    items = [(wifi_color, wifi_up, "WIFI", "W"), (eth_color, eth_up, "ETH", "E")]
    dot, gap, pad = 10, 14, 4
    avail = x1 - x0

    def label_for(item, mode):
        return item[2] if mode == 2 else (item[3] if mode == 1 else "")

    for mode in (2, 1, 0):
        widths = [dot + (pad + d.textlength(label_for(it, mode), font=F_SM)
                         if label_for(it, mode) else 0) for it in items]
        total = sum(widths) + gap * (len(items) - 1)
        if total <= avail or mode == 0:
            break

    x = x0 + max(avail - total, 0) / 2
    cy = y + 8
    for it, w in zip(items, widths):
        color, up = it[0], it[1]
        box = [x, cy - dot / 2, x + dot, cy + dot / 2]
        if up:
            d.ellipse(box, fill=color)
        else:
            d.ellipse(box, outline=DIM, width=2)
        lbl = label_for(it, mode)
        if lbl:
            d.text((x + dot + pad, y), lbl, font=F_SM, fill=FG if up else DIM)
        x += w + gap

def draw_stat_section(d, x0, y0, w, h, label, pct, color, extra_text=None):
    """One stat as a single bar filling the whole section, with the label
    layered top-left and the percentage layered centered on top of it."""
    x1, y1 = x0 + w, y0 + h
    d.rectangle([x0, y0, x1, y1], outline=DIM, width=2)
    fill_w = int((w - 4) * min(max(pct, 0), 100) / 100)
    if fill_w > 0:
        d.rectangle([x0 + 2, y0 + 2, x0 + 2 + fill_w, y1 - 2], fill=color)

    draw_shadow_text(d, (x0 + 10, y0 + 8), label, F_MD)

    pct_text = f"{pct:.0f}%"
    bbox = d.textbbox((0, 0), pct_text, font=F_HUGE)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    px = x0 + (w - tw) / 2 - bbox[0]
    py = y0 + (h - th) / 2 - bbox[1]
    draw_shadow_text(d, (px, py), pct_text, F_HUGE)

    if extra_text:
        bbox2 = d.textbbox((0, 0), extra_text, font=F_SM)
        tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
        draw_shadow_text(d, (x1 - 10 - tw2, y1 - 8 - th2), extra_text, F_SM)


def pct_color(pct):
    return OK_GREEN if pct < 60 else WARN_YELLOW if pct < 85 else BAD_RED


def status_dot_color(status):
    if status in RED_STATUSES:
        return BAD_RED
    if status in YELLOW_STATUSES:
        return WARN_YELLOW
    return OK_GREEN

# ---- PAGES ------------------------------------------------------------------

def draw_home():
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)
    pad = 16
    bar_w = WIDTH - pad * 2
    sec_h = 92
    gap = 8

    time_text = time.strftime("%H:%M:%S")
    d.text((pad, 6), time_text, font=F_SM, fill=DIM)
    tw = d.textlength(time_text, font=F_SM)

    hostname = fit_text(d, socket.gethostname(), F_SM, WIDTH / 3)
    hw = d.textlength(hostname, font=F_SM)
    d.text((WIDTH - pad - hw, 6), hostname, font=F_SM, fill=DIM)

    draw_net_status(d, pad + tw + 10, WIDTH - pad - hw - 10, 6)
    
    #d.text((pad, 6), time.strftime("%H:%M:%S"), font=F_SM, fill=DIM)
    #hostname = socket.gethostname()
    #hbbox = d.textbbox((0, 0), hostname, font=F_SM)
    #hw = hbbox[2] - hbbox[0]
    #d.text((WIDTH - pad - hw, 6), hostname, font=F_SM, fill=DIM)

    disks = get_disks()
    rows = max(1, min(len(disks), MAX_DISK_ROWS))
    sec_h = 92 if rows <= 2 else 72

    y = 30
    cpu = psutil.cpu_percent(interval=None)
    draw_stat_section(d, pad, y, bar_w, sec_h, "CPU", cpu, pct_color(cpu))

    y += sec_h + gap
    mem = psutil.virtual_memory()
    draw_stat_section(d, pad, y, bar_w, sec_h, "MEM", mem.percent, pct_color(mem.percent),
                       extra_text=f"{human_bytes(mem.used)}/{human_bytes(mem.total)}")

    y += sec_h + gap
    disk_bottom = HOME_DOCKER_BUTTON[1] - 14
    row_h = (disk_bottom - y - gap * (rows - 1)) / rows

    if not disks:
        d.rectangle([pad, y, pad + bar_w, disk_bottom], outline=DIM, width=2)
        d.text((pad + 10, y + 8), "no disks found", font=F_MD, fill=DIM)
    elif len(disks) == 1:
        mp, usage = disks[0]
        draw_stat_section(d, pad, y, bar_w, int(row_h), "DISK", usage.percent,
                           pct_color(usage.percent),
                           extra_text=f"{human_bytes(usage.used)}/{human_bytes(usage.total)}")
    else:
        if len(disks) > MAX_DISK_ROWS:
            shown, hidden = disks[:MAX_DISK_ROWS - 1], len(disks) - (MAX_DISK_ROWS - 1)
        else:
            shown, hidden = disks, 0
        for i, (mp, usage) in enumerate(shown):
            draw_disk_row(d, pad, int(y + i * (row_h + gap)), bar_w, int(row_h), mp, usage)
        if hidden:
            hy = int(y + len(shown) * (row_h + gap))
            d.text((pad + 10, hy + 4), f"+{hidden} more", font=F_SM, fill=DIM)

    # Docker status button — big, tappable, colored by worst state.
    containers = get_containers()
    color, label = worst_docker_status(containers)
    bx0, by0, bx1, by1 = HOME_DOCKER_BUTTON
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=16, fill=color)
    d.text((bx0 + 20, by0 + 18), "DOCKER", font=F_LG, fill=BG)
    d.text((bx0 + 20, by0 + 54), label, font=F_MD, fill=BG)
    d.text((bx0 + 20, by1 - 30), "tap for details \u2192", font=F_SM, fill=BG)

    return img


def draw_docker():
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)

    bx0, by0, bx1, by1 = DOCKER_BACK_BUTTON
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=10, outline=ACCENT, width=2)
    d.text((bx0 + 14, by0 + 12), "< BACK", font=F_MD, fill=ACCENT)

    d.text((16, 74), "CONTAINERS", font=F_LG, fill=FG)
    d.line([(16, 108), (WIDTH - 16, 108)], fill=DIM)

    containers = get_containers()
    cy = 122
    row_h = 34
    if containers is None:
        d.text((16, cy), "docker unavailable", font=F_MD, fill=BAD_RED)
    elif not containers:
        d.text((16, cy), "no containers", font=F_MD, fill=DIM)
    else:
        for name, status in containers:
            if cy > HEIGHT - row_h:
                d.text((16, cy), "...", font=F_SM, fill=DIM)
                break
            d.ellipse([16, cy + 4, 30, cy + 18], fill=status_dot_color(status))
            label = name if len(name) <= 18 else name[:17] + "\u2026"
            d.text((40, cy), label, font=F_MD, fill=FG)
            d.text((40, cy + 22), status, font=F_SM, fill=DIM)
            cy += row_h + 14

    return img

# ---- TOUCH HANDLING ---------------------------------------------------------

def find_touch_device():
    if evdev is None:
        return None
    if TOUCH_DEVICE:
        try:
            return evdev.InputDevice(TOUCH_DEVICE)
        except OSError:
            print(f"Couldn't open configured TOUCH_DEVICE={TOUCH_DEVICE}")
            return None
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        caps = dev.capabilities().get(ecodes.EV_ABS, [])
        abs_codes = [c for c, _ in caps]
        if ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes:
            print(f"Using touch device: {dev.path} ({dev.name})")
            return dev
    print("No touch device found — touch input disabled.")
    return None


def in_rect(px, py, rect):
    x0, y0, x1, y1 = rect
    return x0 <= px <= x1 and y0 <= py <= y1


def handle_tap(px, py):
    global current_page
    with state_lock:
        if current_page == "home" and in_rect(px, py, HOME_DOCKER_BUTTON):
            current_page = "docker"
            update_event.set()
        elif current_page == "docker" and in_rect(px, py, DOCKER_BACK_BUTTON):
            current_page = "home"
            update_event.set()


def touch_loop():
    dev = find_touch_device()
    print(f"device: {dev}")
    if dev is None:
        return

    x_info = dev.absinfo(ecodes.ABS_X)
    y_info = dev.absinfo(ecodes.ABS_Y)
    x_min, x_max = x_info.min, x_info.max
    y_min, y_max = y_info.min, y_info.max
    print(f"Touch calibration: X[{x_min},{x_max}] Y[{y_min},{y_max}]")

    raw_x = raw_y = 0
    for event in dev.read_loop():
        #print(f"Touch event: {event}")
        if event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_X:
                raw_x = event.value
            elif event.code == ecodes.ABS_Y:
                raw_y = event.value
        elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
            if event.value == 0:  # release = completed tap
                nx = (raw_x - x_min) / max(x_max - x_min, 1)
                ny = (raw_y - y_min) / max(y_max - y_min, 1)
                if TOUCH_SWAP_XY:
                    nx, ny = ny, nx
                if TOUCH_INVERT_X:
                    nx = 1 - nx
                if TOUCH_INVERT_Y:
                    ny = 1 - ny
                handle_tap(int(nx * WIDTH), int(ny * HEIGHT))

# ---- MAIN --------------------------------------------------------------------

def main():
    psutil.cpu_percent(interval=None)  # prime first reading

    print(get_disks())

    if evdev is not None:
        threading.Thread(target=touch_loop, daemon=True).start()
    else:
        print("evdev not installed — touch input disabled. "
              "pip3 install evdev --break-system-packages")

    while True:
        start = time.time()
        update_event.clear()
        with state_lock:
            page = current_page
        try:
            img = draw_home() if page == "home" else draw_docker()
            push_frame(img)
        except FileNotFoundError:
            print(f"{FB_DEVICE} not found — check the device path")
            time.sleep(5)
            continue
        elapsed = time.time() - start
        update_event.wait(timeout=max(REFRESH_SECONDS - elapsed, 0))


if __name__ == "__main__":
    main()
