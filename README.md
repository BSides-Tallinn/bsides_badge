# BSides Tallinn badge

MicroPython firmware and hardware documentation for the ESP32-C3 BSides Tallinn
badge.

## Supported hardware

| Badge version | OLED address | SELECT | Battery measurement |
| --- | --- | --- | --- |
| `2025_prototype` | `0x3D` | GPIO4 | No |
| `2025` | `0x3C` | GPIO4 | No |
| `2026` | `0x3C` | GPIO10 | GPIO4 / ADC1_CH4 |

The 2026 battery input uses the schematic's 100 kΩ / 20 kΩ divider. The status
screen multiplies the ADC voltage by six and estimates LiPo state of charge from
a rough resting-voltage curve. Charging and LED load can make that percentage
inaccurate.

- [2025 schematic](./hardware/BSides_2025_badge_v1.1_schematics.pdf)
- [2026 schematic](./hardware/BSides_2026_badge_v1.2_schematics.pdf)

Common hardware: ESP32-C3FH4 with 4 MB flash, 128x64 SSD1306 OLED, 16 WS2812B
LEDs, Wi-Fi/Bluetooth, and USB-C flashing/charging.

## Badge configuration

Runtime settings are stored in `/badge.json` on the badge:

```json
{
  "badge_version": "2026",
  "device_id": "A1B2C3D4E5F6",
  "holder_name": "Badge Holder",
  "git_commit": "0123abcd main",
  "params": {
    "Brightness": 10,
    "Hue": 180,
    "Saturation": 100,
    "Speed": 30,
    "Light_effect": 0,
    "SnakeHighScore": 0,
    "PacmanHighScore": 0,
    "TetrisHighScore": 0
  }
}
```

On first boot, firmware creates a random device ID when needed. Existing
`params.json`, `id.txt`, and `yourname.txt` files are migrated into `badge.json`
and removed. The upload tool also preserves those legacy values while upgrading
an existing badge.

Open **Menu -> Badge -> Status** to see the ID, hardware version, uploaded git
commit and branch. A 2026 badge also shows battery voltage and approximate state
of charge.

When **Menu -> Badge setup -> Fetch Name** is selected, the badge connects to
Wi-Fi, synchronizes its RTC from NTP, and briefly displays the resulting UTC
date and time. It then verifies `badge.bsides.ee` using the bundled ISRG Root X1
certificate before requesting the name. The fetch runs in a lightweight mode
so mbedTLS has enough contiguous memory to validate the complete certificate
chain. NTP failures and TLS certificate or hostname validation failures stop
the request and are shown on the display.
While this procedure is running, BACK cancels it, turns off Wi-Fi, and returns
to the badge menu. The DHCP hostname advertised to the router is
`bsides26-<device ID>`.
After a Wi-Fi, NTP, TLS connection, or certificate failure, SELECT or NEXT
retries the procedure from the Wi-Fi connection stage without leaving the
special Fetch Name mode. BACK still exits from the error screen.

## Badge management tool

Use Python 3.10 or newer on Windows, Linux, or macOS. One Python tool is used so
port detection, filtering, configuration migration, and release discovery stay
consistent across platforms.

(Linux) Make sure to add your user to `dialout` group to access the hardware serial port. Log-out/in or restart after this command.
```console
sudo usermod -aG dialout "$USER"
```

Initialize the workstation. This installs missing `esptool` and `mpremote`
packages and downloads the newest stable `ESP32_GENERIC_C3` MicroPython image:

```console
python scripts/badge.py init
```

If `init` fails on your machine, you can try to install `esptool` and `mpremote` via `pipx`.

Erase the chip, flash that image, and upload the application:

```console
python scripts/badge.py flash --badge-version 2026
```

When reachable before erasing, the tool preserves the badge ID, holder name,
and saved parameters. Add `--holder-name "Ada Lovelace"` to set the name during
either `flash` or `upload`. Add `--wipe` to either command to skip reading and
preserving existing settings. This saves time when flashing a brand-new or
already empty badge and creates `badge.json` from the repository defaults.

Upload only application files:

```console
python scripts/badge.py upload --badge-version 2025
```

Set or change the holder's name:

```console
python scripts/badge.py name "Ada Lovelace"
```

Delete every file from the MicroPython filesystem (not recoverable):

```console
python scripts/badge.py delete
```

The tool auto-detects a likely ESP32 serial port. If detection is ambiguous,
pass `--port COM4`, `--port /dev/ttyACM0`, or the relevant macOS
`/dev/cu.usbmodem*` path after the command name. Upload and name operations check
the installed MicroPython version against the latest stable release by default;
use `--skip-version-check` only when working offline. `__pycache__` directories,
`*.pyc`, `.DS_Store`, `requirements.txt`, and the template
`software/badge.json` are never copied as ordinary files. Instead, the tool
generates `badge.json`, preserving device settings unless `--wipe` is used and
adding the selected hardware version plus the current eight-character git hash
and branch.

If `mpremote` cannot interrupt the running application, hold SELECT while
resetting or power-cycling the badge. The correct SELECT pin is chosen from
`badge.json` on both 2025 and 2026 hardware.

Run `python scripts/badge.py --help` or a subcommand with `--help` for all
options. After a successful operation on a 2026 badge, the tool prints the
currently measured battery voltage as its final output line. It adds
`WARNING!!` when the voltage is below 3.8 V or above 4.2 V.

## Games

Open **Menu -> Games** to select an installed game. The menu discovers Python
files in `software/games` at runtime. Pacman, Snake, Tetris, and two-player Pong
are included.

To add a game, place a `.py` file in `software/games`. It must export:

```python
GAME_NAME = "My game"
GameScreen = MyGameScreen
```

`GameScreen(oled)` must provide `render()` and async `handle_button(btn)`
methods. Set `manages_own_render = True` when the game owns an animation loop.
On exit, return `bsides.GamesScreen(oled)`.

### Pacman

Single player on a 32x12 cell maze with three ghosts, power pellets, and a
wrap-around tunnel. Turns are relative to the current heading and are applied
at the first cell where they fit:

- NEXT turns clockwise, PREV turns counter-clockwise. Press the same button
  twice to reverse.
- SELECT pauses and resumes, or restarts after game over.
- BACK exits to the Games menu.

Dots score 10, power pellets 50, and eaten ghosts 200, 400, 800, and 1600 in a
row. An extra life is awarded at 10000 points. Clearing the maze starts the next
level with faster ghosts. The high score is stored as `PacmanHighScore` in
`badge.json`.

### Tetris

A 10x15 well in the middle of the display with the next piece, level, and line
count on the left and the score and high score on the right. A dot marks where
the falling piece will land.

- NEXT moves right and PREV moves left. Hold either to keep sliding.
- SELECT rotates. Hold SELECT for a soft drop.
- BACK exits to the Games menu. SELECT restarts after game over.

Clearing one to four lines at once scores 40, 100, 300, or 1200 times the
level, and each soft-dropped row adds one point. The level rises every ten
lines and gravity speeds up with it. The high score is stored as
`TetrisHighScore` in `badge.json`.

### Pong link

Pong uses UART1 on GPIO20/GPIO21. Cross-connect TX to RX in both directions and
connect GND between badges:

- Badge A TX (pin 28) -> Badge B RX (pin 27)
- Badge B TX (pin 28) -> Badge A RX (pin 27)
- Badge A GND -> Badge B GND

Open Pong on both badges. The higher device ID becomes host; after a three-second
countdown, the match lasts 60 seconds. NEXT moves up, SELECT moves down, and
BACK exits.
