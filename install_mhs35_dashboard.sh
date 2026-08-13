#!/usr/bin/env bash
#
# Installs the MHS-3.5 stats dashboard on this Pi.
# Run from the directory containing mhs35_dashboard.py and
# mhs35-dashboard.service, e.g.:
#   sudo ./install_mhs35_dashboard.sh
#
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root (it installs packages, writes to /opt and /etc/systemd)."
    echo "Re-run with: sudo $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_PY="$SCRIPT_DIR/mhs35_dashboard.py"
SRC_SERVICE="$SCRIPT_DIR/mhs35-dashboard.service"

for f in "$SRC_PY" "$SRC_SERVICE"; do
    if [[ ! -f "$f" ]]; then
        echo "Expected to find $f next to this script — aborting."
        exit 1
    fi
done

prompt() {
    # prompt "Question text" "default" -> echoes chosen value
    local question="$1" default="$2" answer
    read -rp "$question [$default]: " answer
    echo "${answer:-$default}"
}

echo "== MHS-3.5 dashboard installer =="
echo

echo "--- Installing dependencies ---"
apt update -qq
apt install -y python3-evdev python3-pil python3-numpy python3-psutil python3-docker

echo
echo "--- Framebuffer configuration ---"
FB_DEVICE=$(prompt "Framebuffer device" "/dev/fb0")

if command -v fbset >/dev/null 2>&1 && [[ -e "$FB_DEVICE" ]]; then
    echo "Detected info for $FB_DEVICE:"
    if ! fbset -fb "$FB_DEVICE" -i; then
        echo
        echo "fbset couldn't read $FB_DEVICE even though the device file exists —"
        echo "this usually means it isn't a working framebuffer (wrong device, missing"
        echo "overlay, or bad config). Fix that before continuing rather than guessing"
        echo "at values for a display that likely won't work."
        exit 1
    fi
else
    echo "fbset not available or $FB_DEVICE doesn't exist yet — enter values manually."
fi

WIDTH=$(prompt "Screen width (pixels)" "320")
HEIGHT=$(prompt "Screen height (pixels)" "480")
ROTATE=$(prompt "Software rotation (0, 90, 180, or 270)" "0")

echo
echo "--- Touch input ---"
echo "Leave blank to auto-detect the touch device at runtime (recommended)."
TOUCH_DEVICE=$(prompt "Touch device path (blank = auto-detect)" "")

echo
echo "--- Console handling ---"
echo "Boot messages print to /proc/cmdline's fbcon-mapped tty; look for fbcon=map:N there,"
echo "or just leave the default of tty1 if unsure — it's correct on most single-display Pis."
GETTY_TTY=$(prompt "Which tty should have its login prompt disabled" "tty1")

echo
echo "--- Install location ---"
INSTALL_DIR=$(prompt "Install directory" "/opt/mhs35-dashboard")

# ---- Install the script unmodified, config lives in a separate .env file --
mkdir -p "$INSTALL_DIR"
DEST_PY="$INSTALL_DIR/mhs35_dashboard.py"
cp "$SRC_PY" "$DEST_PY"

DEST_ENV="$INSTALL_DIR/mhs35-dashboard.env"
cat > "$DEST_ENV" << EOF
# Config for mhs35_dashboard.py — edit freely and run
# 'systemctl restart mhs35-dashboard' to apply. Safe to delete;
# the script falls back to its built-in defaults if this is missing.
MHS35_FB_DEVICE=$FB_DEVICE
MHS35_WIDTH=$WIDTH
MHS35_HEIGHT=$HEIGHT
MHS35_ROTATE=$ROTATE
MHS35_TOUCH_DEVICE=$TOUCH_DEVICE
EOF

echo "Installed script to $DEST_PY"
echo "Wrote config to $DEST_ENV"

# ---- Service file ----------------------------------------------------------
DEST_SERVICE_NAME="mhs35-dashboard.service"
sed -e "s|/opt/mhs35-dashboard/mhs35_dashboard.py|$DEST_PY|" \
    -e "s|/opt/mhs35-dashboard/mhs35-dashboard.env|$DEST_ENV|" \
    "$SRC_SERVICE" > "/etc/systemd/system/$DEST_SERVICE_NAME"
echo "Installed $DEST_SERVICE_NAME to /etc/systemd/system/"

# ---- Console / getty ---------------------------------------------------------
echo
echo "--- Disabling getty@$GETTY_TTY.service ---"
systemctl disable --now "getty@$GETTY_TTY.service" || echo "  (couldn't disable getty@$GETTY_TTY — check the tty name and do this manually if needed)"

# ---- Start service -----------------------------------------------------------
echo
echo "--- Starting the dashboard service ---"
systemctl daemon-reload
systemctl enable --now "$DEST_SERVICE_NAME"

echo
echo "== Done =="
echo "Check status with:   systemctl status $DEST_SERVICE_NAME"
echo "Watch live logs with: journalctl -u $DEST_SERVICE_NAME -f"
echo
echo "If the touch overlay isn't set up yet, that's a one-time /boot/firmware/config.txt"
echo "change and reboot — this script doesn't touch that since it's board-specific"
echo "(on nexus this needed dtoverlay=mhs35,penirq=17,... rather than the generic ads7846 name)."
