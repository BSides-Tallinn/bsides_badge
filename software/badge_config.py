import json
import os
import ubinascii
import urandom


BADGE_FILENAME = "badge.json"
SUPPORTED_BADGE_VERSIONS = ("2025_prototype", "2025", "2026")

HARDWARE = {
    "2025_prototype": {
        "oled_address": 0x3D,
        "select_pin": 4,
        "battery_pin": None,
    },
    "2025": {
        "oled_address": 0x3C,
        "select_pin": 4,
        "battery_pin": None,
    },
    "2026": {
        "oled_address": 0x3C,
        "select_pin": 10,
        "battery_pin": 4,
    },
}

DEFAULT_PARAMS = {
    "Brightness": 10,
    "Hue": 180,
    "Saturation": 100,
    "Speed": 30,
    "Light_effect": 0,
    "SnakeHighScore": 0,
}


def default_config():
    return {
        "badge_version": "2025",
        "device_id": "",
        "holder_name": "",
        "git_commit": "unknown",
        "params": dict(DEFAULT_PARAMS),
    }


def is_valid_device_id(value):
    if not isinstance(value, str) or len(value) != 12:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _read_text(filename):
    try:
        with open(filename, "r") as stream:
            return stream.read().strip()
    except OSError:
        return ""


def _read_json(filename):
    try:
        with open(filename, "r") as stream:
            value = json.load(stream)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_badge_config(config):
    with open(BADGE_FILENAME, "w") as stream:
        json.dump(config, stream)


def _remove_legacy_files():
    for filename in ("params.json", "id.txt", "yourname.txt"):
        try:
            os.remove(filename)
        except OSError:
            pass


def load_badge_config():
    """Load badge.json, migrating the three legacy settings files if needed."""
    config = default_config()
    stored = _read_json(BADGE_FILENAME)
    changed = not stored

    for key in ("badge_version", "device_id", "holder_name", "git_commit"):
        if key in stored:
            config[key] = stored[key]
    if isinstance(stored.get("params"), dict):
        config["params"].update(stored["params"])

    # Migration keeps already-issued IDs and holder names on upgraded badges.
    legacy_params = _read_json("params.json")
    if legacy_params:
        config["params"].update(legacy_params)
        changed = True
    legacy_id = _read_text("id.txt").upper()
    if not is_valid_device_id(config["device_id"]) and is_valid_device_id(legacy_id):
        config["device_id"] = legacy_id
        changed = True
    legacy_name = _read_text("yourname.txt")
    if not config["holder_name"] and legacy_name:
        config["holder_name"] = legacy_name
        changed = True

    if config["badge_version"] not in SUPPORTED_BADGE_VERSIONS:
        config["badge_version"] = "2025"
        changed = True
    if not is_valid_device_id(config["device_id"]):
        random_bytes = bytes([urandom.getrandbits(8) for _ in range(6)])
        config["device_id"] = ubinascii.hexlify(random_bytes).decode().upper()
        changed = True

    if changed:
        save_badge_config(config)
        _remove_legacy_files()
    return config


def hardware_for(version):
    return HARDWARE.get(version, HARDWARE["2025"])
