# BSides Tallinn badge

MicroPython firmware and hardware documentation for the ESP32-C3 BSides Tallinn
badge.

## Supported hardware

| Badge version | OLED address | SELECT | Battery measurement | Appearance |
| --- | --- | --- | --- | --- |
| `2025_prototype` | `0x3D` | GPIO4 | No | Cylindrical battery, empty back side |
| `2025` | `0x3C` | GPIO4 | No | Cylindrical battery, "BSIDES #5" and wolf on the back side |
| `2026` | `0x3C` | GPIO10 | GPIO4 / ADC1_CH4 | Flat LiPo battery, "BSIDES #6" and wolf on the back side |

The 2026 battery input uses the schematic's 100 kΩ / 20 kΩ divider. The status
screen multiplies the ADC voltage by six and estimates LiPo state of charge from
a rough resting-voltage curve. Charging and LED load can make that percentage
inaccurate.

- [2025 schematic](./hardware/BSides_2025_badge_v1.1_schematics.pdf)
- [2026 schematic](./hardware/BSides_2026_badge_v1.2_schematics.pdf)

Common hardware: ESP32-C3FH4 with 4 MB flash, 128x64 SSD1306 OLED, 16 WS2812B
LEDs, Wi-Fi/Bluetooth, and USB-C flashing/charging.

On all badge versions, **Lights -> Plug-in -> Effects** controls the two plug-in LEDs
on GPIO6 and GPIO7. **Breathe** (default), **Blink**, and **Police** run in
opposite phases; **On** lights both LEDs and **Off** turns both off. The choice
is saved when leaving Lights and is independent of the NeoPixel settings.

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
    "Plugin_effect": 0,
    "SnakeHighScore": 0,
    "PacmanHighScore": 0,
    "TetrisHighScore": 0,
    "FlappyHighScore": 0
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
packages and downloads the newest stable `ESP32_GENERIC_C3` MicroPython image.
If `init` fails on your Linux machine, try installing `esptool` and `mpremote` via `pipx`.

```console
python scripts/badge.py init
```

The following commands will try to connect to ESP32 chip on the badge via
the USB-C connector (the badge has has to be turned on with small switch SW2
near the SELECT button).

Erase the chip, flash that image, and upload the application:

```console
python scripts/badge.py flash --badge-version 2026
```

The same with full wipe (no previous settings restored)

```console
python scripts/badge.py flash --wipe --badge-version 2026
```

When reachable before erasing, the tool preserves the badge ID, holder name,
and saved parameters. Add `--holder-name "Ada Lovelace"` to set the name during
either `flash` or `upload`. Add `--wipe` to either command to skip reading and
preserving existing settings. This saves time when flashing a brand-new or
already empty badge and creates `badge.json` from the repository defaults.

Upload only application files:

```console
python scripts/badge.py upload --badge-version 2026
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

If your badge is somehow bricked (wrong version flashed, etc.) and does not
respond to `esptool`, try  holding down BACK button (GPIO9) while resetting
or turning your badge on. This will start ESP32 in bootloader mode.

Run `python scripts/badge.py --help` or a subcommand with `--help` for all
options. After a successful operation on a 2026 badge, the tool prints the
currently measured battery voltage as its final output line. It adds
`WARNING!!` when the voltage is below 3.8 V or above 4.2 V.

## Games

Open **Menu -> Games** to select an installed game. The menu discovers Python
files in `software/games` at runtime. Flappy Bird, Pacman, Snake, Tetris, and
the two-player games Pong and Tic-tac-toe are included.

To add a game, place a `.py` file in `software/games`. It must export:

```python
GAME_NAME = "My game"
GameScreen = MyGameScreen
```

`GameScreen(oled)` must provide `render()` and async `handle_button(btn)`
methods. Set `manages_own_render = True` when the game owns an animation loop.
On exit, return `bsides.GamesScreen(oled)`.

### Flappy Bird

Fly through the gaps between scrolling pipes. Each pipe passed scores one
point, and the gaps narrow as the score rises. Touching a pipe or the ground
ends the run; the top of the playfield is safe.

- SELECT or NEXT flaps. The first flap starts the run. Holding NEXT does not
  repeat.
- PREV pauses and resumes. A flap also resumes.
- BACK exits to the Games menu. SELECT restarts after game over.

The high score is stored as `FlappyHighScore` in `badge.json`.

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

### Tic-tac-toe link

Tic-tac-toe is the second two-player game and uses the same cable as Pong: TX
to RX in both directions plus GND. Open Tic-tac-toe on both badges. The higher
device ID becomes host, owns the board, and plays X. The other badge plays O.
The first game starts with X and the starter alternates after that.

- NEXT and PREV move the blinking cursor to the next or previous empty cell.
  A small dot shows where the other player's cursor is.
- SELECT places your mark on your turn, and starts a new game once one is
  finished. Either player can start the new game.
- BACK exits.

The side panel shows your mark, whose turn it is, and the session score as your
wins against your losses.

Every message carries a checksum, the host repeats the whole board a few times
per second, and the guest repeats a move until the board shows it. A dropped or
garbled line therefore cannot desynchronise the game. If the cable is
unplugged, both badges show "Link lost" and resume the same game when it is
plugged back in. If one badge leaves and reopens the game, a returning guest
gets the board back, while a returning host starts a fresh game. Pong traffic
on the other end is ignored, so both badges must run the same game.
