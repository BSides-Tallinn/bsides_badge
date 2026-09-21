"""Low-memory, certificate-verified badge name fetch mode."""

import gc
import json
import os
import socket
import ssl
import struct
import time
import uasyncio as asyncio
from machine import I2C, Pin, RTC, reset

import network
import ssd1306
from badge_config import format_device_id, hardware_for, load_badge_config, save_badge_config


SSID = "bsides-badge"
PASSWORD = "bsidestallinn"
HOST = "badge.bsides.ee"
TLS_CA_FILE = "/certs/isrg-root-x1.pem"
NTP_HOST = "pool.ntp.org"
NTP_TIMEOUT_MS = 5000
NETWORK_TIMEOUT_MS = 10000
NEXT_PIN = 5
BACK_PIN = 9

config = load_badge_config()
device_id = config["device_id"]
hardware = hardware_for(config["badge_version"])
oled = ssd1306.SSD1306_I2C(
    128, 64, I2C(0, scl=Pin(1), sda=Pin(0)),
    addr=hardware["oled_address"])
back = Pin(BACK_PIN, Pin.IN)
next_button = Pin(NEXT_PIN, Pin.IN)
select_button = Pin(hardware["select_pin"], Pin.IN)
wlan = None
exit_requested = False


class CertificateError(Exception):
    pass


class NetworkError(Exception):
    pass


def show(message):
    """Show a short wrapped status using the framebuffer's built-in font."""
    oled.fill(0)
    oled.text(format_device_id(device_id), 0, 0, 1)
    words = str(message).replace("\n", " \n ").split(" ")
    lines = []
    line = ""
    for word in words:
        if word == "\n":
            lines.append(line)
            line = ""
            continue
        while len(word) > 16:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:16])
            word = word[16:]
        candidate = (line + " " + word).strip()
        if len(candidate) <= 16:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    for row, text in enumerate(lines[:6]):
        oled.text(text, 0, 10 + row * 9, 1)
    oled.show()


def disconnect():
    global wlan
    if wlan is None:
        return
    try:
        wlan.disconnect()
    except OSError:
        pass
    wlan.active(False)


async def connect_wifi():
    global wlan
    wlan = network.WLAN(network.STA_IF)
    network.hostname("bsides26-{}".format(device_id))
    wlan.active(True)
    if wlan.isconnected():
        return
    wlan.connect(SSID, PASSWORD)
    for _ in range(100):
        if wlan.isconnected():
            return
        await asyncio.sleep_ms(100)
    raise RuntimeError("Could not connect")


async def update_time():
    query = bytearray(48)
    query[0] = 0x23
    query[40:48] = os.urandom(8)
    addr = socket.getaddrinfo(NTP_HOST, 123, 0, socket.SOCK_DGRAM)[0][-1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        sock.sendto(query, addr)
        deadline = time.ticks_add(time.ticks_ms(), NTP_TIMEOUT_MS)
        response = None
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                response = sock.recv(48)
                break
            except OSError:
                await asyncio.sleep_ms(50)
        if response is None:
            raise RuntimeError("Timed out")
    finally:
        sock.close()

    if (len(response) != 48 or response[24:32] != query[40:48]
            or response[0] & 7 != 4 or response[0] >> 6 == 3
            or not 1 <= response[1] <= 15):
        raise RuntimeError("Invalid response")

    ntp_seconds = struct.unpack("!I", response[40:44])[0]
    if ntp_seconds < 3913056000:
        ntp_seconds += 0x100000000
    epoch_year = time.gmtime(0)[0]
    if epoch_year == 2000:
        ntp_delta = 3155673600
    elif epoch_year == 1970:
        ntp_delta = 2208988800
    else:
        raise RuntimeError("Unsupported epoch")
    now = time.gmtime(ntp_seconds - ntp_delta)
    if now[0] < 2024 or now[0] > 2100:
        raise RuntimeError("Invalid time")
    RTC().datetime((now[0], now[1], now[2], now[6] + 1,
                    now[3], now[4], now[5], 0))
    return ("{:04d}-{:02d}-{:02d}\n{:02d}:{:02d}:{:02d} UTC"
            .format(now[0], now[1], now[2], now[3], now[4], now[5]))


async def fetch_name():
    stream = None
    try:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(cafile=TLS_CA_FILE)
        except Exception as error:
            raise CertificateError(str(error))

        try:
            stream, _ = await asyncio.wait_for_ms(
                asyncio.open_connection(
                    HOST, 443, ssl=context, server_hostname=HOST),
                NETWORK_TIMEOUT_MS)
            request = "GET /getname/{} HTTP/1.0\r\nHost: {}\r\n\r\n".format(
                device_id, HOST)
            stream.write(request.encode())
            # MicroPython performs the asynchronous TLS handshake here.
            await asyncio.wait_for_ms(stream.drain(), NETWORK_TIMEOUT_MS)
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            raise CertificateError(str(error))
        except Exception as error:
            raise NetworkError(str(error))

        show("Certificate OK\nFetching name...")
        response = b""
        try:
            while True:
                chunk = await asyncio.wait_for_ms(
                    stream.read(512), NETWORK_TIMEOUT_MS)
                if not chunk:
                    break
                response += chunk
                if len(response) > 4096:
                    raise RuntimeError("Response too large")
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            raise
        except Exception as error:
            raise NetworkError(str(error))
    finally:
        if stream:
            stream.close()
            try:
                await stream.wait_closed()
            except Exception:
                pass

    try:
        data = json.loads(response.split(b"\r\n\r\n", 1)[-1])
    except ValueError:
        raise RuntimeError("Invalid response")
    if "error" in data:
        raise RuntimeError(str(data.get("error", "Server error")))
    if data.get("id", "").upper() != device_id.upper() or "name" not in data:
        raise RuntimeError("Unexpected response")
    return data["name"].strip()


async def run_fetch():
    show("Connecting WiFi...")
    try:
        await connect_wifi()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print("WiFi error:", error)
        show("WiFi error\nSELECT or NEXT\nto retry\nBACK to exit")
        return True

    show("Updating UTC time...")
    try:
        current_time = await update_time()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print("NTP error:", error)
        show("Internet error\nSELECT or NEXT\nto retry\nBACK to exit")
        return True
    show(current_time)
    await asyncio.sleep(2)

    show("Checking certificate...")
    gc.collect()
    try:
        name = await fetch_name()
    except asyncio.CancelledError:
        raise
    except CertificateError as error:
        print("Certificate check error:", error)
        show("Certificate error\nSELECT or NEXT\nto retry\nBACK to exit")
        return True
    except NetworkError as error:
        print("Network error:", error)
        show("Internet error\nSELECT or NEXT\nto retry\nBACK to exit")
        return True
    except Exception as error:
        print("Fetch error:", error)
        show("Fetch error: {}\nBACK to exit".format(error))
        return False

    config["holder_name"] = name
    try:
        save_badge_config(config)
    except OSError as error:
        print("Save error:", error)
        show("Name fetched; save error")
        return False
    show("Name: {}\nBACK to exit".format(name))
    return False


async def watch_back(fetch_task):
    global exit_requested
    while True:
        if back.value() == 0:
            exit_requested = True
            fetch_task.cancel()
            return
        await asyncio.sleep_ms(30)


async def wait_for_action(allow_retry):
    """Wait for BACK, or SELECT/NEXT when the failed operation is retryable."""
    while True:
        if back.value() == 0:
            return "back"
        if allow_retry and (select_button.value() == 0
                            or next_button.value() == 0):
            # Do not carry the retry press into the newly-started attempt.
            while select_button.value() == 0 or next_button.value() == 0:
                if back.value() == 0:
                    return "back"
                await asyncio.sleep_ms(30)
            return "retry"
        await asyncio.sleep_ms(30)


def return_to_badge():
    disconnect()
    RTC().memory(b"")
    show("Returning...")
    time.sleep_ms(100)
    reset()


async def main():
    global exit_requested
    while True:
        exit_requested = False
        fetch_task = asyncio.create_task(run_fetch())
        back_task = asyncio.create_task(watch_back(fetch_task))
        retry_allowed = False
        try:
            retry_allowed = await fetch_task
        except asyncio.CancelledError:
            pass
        finally:
            back_task.cancel()
            disconnect()

        if exit_requested:
            return_to_badge()

        action = await wait_for_action(retry_allowed)
        if action == "back":
            return_to_badge()
        # SELECT or NEXT retries in this lightweight mode without rebooting.


try:
    asyncio.run(main())
finally:
    asyncio.new_event_loop()
