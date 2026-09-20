# BSides 25 badge

## Hardware

ESP32-C3FH4 (4MB flash) with WiFi and Bluetooth

128x64 px OLED display (SSD1306)

16 WS2812B (Neopixel compatible) LEDs

USB-C for flashing/charging

[Schematics](./hardware/BSides_2025_badge_v1.1_schematics.pdf)

## Software

The code in `software` is written in MicroPython and loaded onto the badge via USB-C connector.

Update the code by uploading via `mpremote` or directly via some IDE like [Thonny](https://thonny.org/).

## Device preparation

Install `esptool` and `mpremote`
```
pip install --user esptool mpremote
```

Install [MicroPython](https://micropython.org/download/ESP32_GENERIC_C3).

For BSides 2025: v1.26.1 (2025-09-11)
```
wget https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20250911-v1.26.1.bin
esptool --port <port> erase_flash
esptool --port <port> --baud 921600 write_flash 0 ESP32_GENERIC_C3-20250911-v1.26.1.bin
```

## Copy files to the badge

```
mpremote <port> fs cp -r software/* :/
```

If the code is already running on the badge and `mpremote` does not connect, hold `SELECT` button down while resetting your badge (press `RESET` button or toggling ON/OFF switch).

## Games

Open **Menu -> Games** to select one of the games installed on the badge.
The menu is populated at runtime from Python files in `software/games`, so the
main application does not contain a hard-coded list of games. Snake and Pong
are included.

To add a game, copy one `.py` file into `software/games`. The module must export:

```python
GAME_NAME = "My game"
GameScreen = MyGameScreen
```

`GameScreen(oled)` must provide `render()` and async `handle_button(btn)`
methods. Set `manages_own_render = True` when the game runs and renders from
its own async loop. On exit, return `bsides25.GamesScreen(oled)`.

### Pong (2-player)

Two badges can play Pong against each other over the UART link (hardware UART1 on the UART pads, chip pins 27/28 = GPIO20/GPIO21).

Wiring between the badges (crossed):
- Badge A TX (pin 28) -> Badge B RX (pin 27)
- Badge B TX (pin 28) -> Badge A RX (pin 27)
- Common GND

On both badges open Menu -> Games -> Pong. The badges handshake automatically
and the higher device ID becomes host. They can enter Pong at different times;
a badge waiting on the "No peer found" screen will accept a peer that arrives
later. After a 3 s countdown a 60 s match starts; each player sees their own
paddle on the left. NEXT moves the paddle up and SELECT moves it down; either
button can be held to keep moving.
When time is up, the badge with more goals wins. SELECT = rematch, BACK = exit.

Headless link test on a PC (no badge needed):
```
python3 tests/test_pong.py
```
