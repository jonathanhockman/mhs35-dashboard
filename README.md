# MHS-3.5 Stats Dashboard

A small Python program that draws a live system-stats dashboard directly to
an MHS-3.5 SPI touchscreen's framebuffer — no desktop environment, no X
server, just raw pixels pushed to `/dev/fb0`.

## What it does

Runs as a systemd service and continuously redraws two screens:

**Home screen**
- Big, glanceable CPU / memory / disk usage — each shown as a single bar
  filling the whole section, colored green/yellow/red by threshold, with
  the label and percentage layered on top
- Memory and disk also show used/total (e.g. `128G/932G`)
- A large Docker status button, colored by the **worst** state across all
  containers (red if any container is `exited`/`dead`, yellow if any is
  `restarting`/`paused`/`created`, otherwise green)
- Hostname shown top-right, so the same script/install works unmodified on
  any device
- Tapping the Docker button switches to the Docker detail screen

**Docker screen**
- Full list of containers with name, colored status dot, and status text
- `< BACK` button returns to the home screen

Touch input is handled via `evdev`, auto-detecting the touch device at
runtime.

## Files

| File | Purpose |
|---|---|
| `mhs35_dashboard.py` | The dashboard itself |
| `mhs35-dashboard.service` | systemd unit — runs the script, loads config, unbinds the console from the framebuffer before starting |
| `install_mhs35_dashboard.sh` | Interactive installer — prompts for the device-specific values below and sets everything up |

## Configuration

The script reads its settings from environment variables (with sane
defaults built in), loaded via the systemd service's `EnvironmentFile=`
pointing at `/opt/mhs35-dashboard/mhs35-dashboard.env`. To change a value
after install:

```bash
sudo nano /opt/mhs35-dashboard/mhs35-dashboard.env
sudo systemctl restart mhs35-dashboard
```

| Variable | Default | Meaning |
|---|---|---|
| `MHS35_FB_DEVICE` | `/dev/fb0` | Framebuffer device path |
| `MHS35_WIDTH` | `320` | Screen width in pixels |
| `MHS35_HEIGHT` | `480` | Screen height in pixels |
| `MHS35_ROTATE` | `90` | Software rotation: `0`, `90`, `180`, or `270` |
| `MHS35_TOUCH_DEVICE` | *(blank = auto-detect)* | Explicit touch input device path, e.g. `/dev/input/event3`, if auto-detect picks the wrong one |

A few touch-calibration flags exist in the script itself
(`TOUCH_SWAP_XY`, `TOUCH_INVERT_X`, `TOUCH_INVERT_Y`) for panels that
report X/Y swapped or inverted. These aren't part of the installer prompts
since they're only discoverable by testing on the actual hardware — edit
them directly in `mhs35_dashboard.py` if a panel needs them.

## Installing on a new device

1. Copy all three files to the target Pi, e.g.:
   ```bash
   scp mhs35_dashboard.py mhs35-dashboard.service install_mhs35_dashboard.sh pi@<host>:~/
   ```
2. SSH in and run the installer:
   ```bash
   ssh pi@<host>
   sudo ./install_mhs35_dashboard.sh
   ```
3. Answer the prompts — each one has a sensible default in brackets, and
   pressing Enter accepts it. It will:
   - Install dependencies via `apt` (`python3-evdev`, `python3-pil`,
     `python3-numpy`, `python3-psutil`, `python3-docker` — no pip needed)
   - Show detected framebuffer info (via `fbset`) to help you confirm the
     device path/resolution
   - Write the script and a `.env` config file to `/opt/mhs35-dashboard/`
   - Install and enable the systemd service
   - Disable the console login prompt (`getty`) on the screen's tty so it
     doesn't fight the dashboard for the framebuffer

The installer stops with an error if it finds a framebuffer device that
exists but can't be read — that usually means it's the wrong device path,
or the display driver/overlay isn't set up correctly yet.

## Hardware notes

- The touch controller needs the correct `dtoverlay=` line in
  `/boot/firmware/config.txt`, including `penirq=<BCM GPIO number>` (not
  the header pin number). On nexus, this needed the board-specific
  `dtoverlay=mhs35,penirq=17,...` — the generic `ads7846` overlay name
  did not work. Reboot after adding or changing this line.
- Use `pinout` to look up the BCM GPIO number for the touch controller's
  interrupt pin from its physical header pin number.

## Managing the service

```bash
systemctl status mhs35-dashboard          # check it's running
journalctl -u mhs35-dashboard -f          # watch live logs
sudo systemctl restart mhs35-dashboard    # apply a config change
```