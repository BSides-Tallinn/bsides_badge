#!/usr/bin/env python3
"""Prepare and manage BSides Tallinn ESP32-C3 badges."""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOFTWARE_DIR = ROOT / "software"
DEFAULT_FIRMWARE_DIR = ROOT / ".cache" / "firmware"
DOWNLOAD_PAGE = "https://micropython.org/download/ESP32_GENERIC_C3/"
SUPPORTED_BADGES = ("2025_prototype", "2025", "2026")
SKIPPED_NAMES = {".ds_store", "badge.json", "requirements.txt"}
LEGACY_FILES = ("params.json", "id.txt", "yourname.txt")
OBSOLETE_FILES = ("bsides25.py",)


class BadgeToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Firmware:
    version: str
    date: str
    url: str

    @property
    def filename(self) -> str:
        return urllib.parse.urlsplit(self.url).path.rsplit("/", 1)[-1]


def run(command: list[str], *, check: bool = True, capture: bool = False,
        timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    print("+", subprocess.list2cmdline(command))
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def tool_command(tool: str) -> list[str] | None:
    """Return a command for an importable module or standalone executable."""
    if importlib.util.find_spec(tool) is not None:
        return [sys.executable, "-m", tool]
    executable = shutil.which(tool)
    return [executable] if executable else None


def ensure_tools(install: bool) -> None:
    packages = [package for package in ("esptool", "mpremote")
                if tool_command(package) is None]
    if not packages:
        print("esptool and mpremote are installed.")
        return
    if not install:
        raise BadgeToolError(
            "Missing {}. Run 'python scripts/badge.py init'.".format(
                ", ".join(packages)))
    run([sys.executable, "-m", "pip", "install"] + packages)


def parse_latest_firmware(page: str, base_url: str = DOWNLOAD_PAGE) -> Firmware:
    pattern = re.compile(
        r'''href=["']([^"']*ESP32_GENERIC_C3-(\d{8})-v(\d+(?:\.\d+)+)\.bin)["']''',
        re.IGNORECASE,
    )
    found = []
    for href, date, version in pattern.findall(page):
        found.append((tuple(int(part) for part in version.split(".")), date,
                      Firmware(version, date,
                               urllib.parse.urljoin(base_url, html.unescape(href)))))
    if not found:
        raise BadgeToolError("Could not find a stable ESP32_GENERIC_C3 .bin download.")
    return max(found, key=lambda item: (item[0], item[1]))[2]


def latest_firmware() -> Firmware:
    request = urllib.request.Request(DOWNLOAD_PAGE, headers={"User-Agent": "bsides-badge-tool"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise BadgeToolError("Could not query MicroPython downloads: {}".format(exc))
    return parse_latest_firmware(page)


def download_firmware(firmware: Firmware, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / firmware.filename
    if destination.exists() and destination.stat().st_size:
        print("Using cached firmware {}".format(destination))
        return destination
    print("Downloading MicroPython {}...".format(firmware.version))
    try:
        with urllib.request.urlopen(firmware.url, timeout=60) as response:
            data = response.read()
        destination.write_bytes(data)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise BadgeToolError("Firmware download failed: {}".format(exc))
    return destination


def detect_port(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        from serial.tools import list_ports
    except ImportError:
        print("Serial-port detection unavailable; tools will use auto-detection.")
        return None

    ports = list(list_ports.comports())
    if not ports:
        print("No serial port found; tools will use auto-detection.")
        return None
    likely = [port for port in ports if port.vid == 0x303A or any(
        word in "{} {} {}".format(port.description, port.manufacturer, port.product).lower()
        for word in ("espressif", "esp32", "usb jtag/serial"))]
    candidates = likely or ports
    if len(candidates) == 1:
        print("Detected badge port: {}".format(candidates[0].device))
        return candidates[0].device
    raise BadgeToolError(
        "Multiple serial ports found ({}); pass --port.".format(
            ", ".join(port.device for port in candidates)))


def mpremote_prefix(port: str | None) -> list[str]:
    command = tool_command("mpremote")
    if command is None:
        raise BadgeToolError("mpremote is not installed or available on PATH.")
    if port:
        command += ["connect", port]
    return command


def remote_read(port: str | None, filename: str) -> str:
    result = run(mpremote_prefix(port) + ["fs", "cat", ":/" + filename],
                 check=False, capture=True, timeout=20)
    return result.stdout.strip() if result.returncode == 0 else ""


def extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(text[start:end + 1])
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def load_local_defaults() -> dict[str, Any]:
    return json.loads((SOFTWARE_DIR / "badge.json").read_text(encoding="utf-8"))


def read_remote_config(port: str | None) -> dict[str, Any]:
    config = extract_json(remote_read(port, "badge.json"))
    if config:
        return config

    # Upgrade badges that still have the original three settings files.
    legacy_params = extract_json(remote_read(port, "params.json"))
    legacy_id = remote_read(port, "id.txt").strip().upper()
    legacy_name = remote_read(port, "yourname.txt").strip()
    if legacy_params:
        config["params"] = legacy_params
    if re.fullmatch(r"[0-9A-F]{12}", legacy_id):
        config["device_id"] = legacy_id
    if legacy_name:
        config["holder_name"] = legacy_name
    return config


def existing_config(port: str | None, wipe: bool) -> dict[str, Any]:
    """Read settings to preserve unless a fresh configuration was requested."""
    return {} if wipe else read_remote_config(port)


def git_commit_info() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"], cwd=ROOT,
            check=True, text=True, capture_output=True).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=ROOT,
            check=True, text=True, capture_output=True).stdout.strip() or "detached"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            check=True, text=True, capture_output=True).stdout.strip()
        if dirty:
            print("Warning: the working tree has uncommitted changes; git info identifies HEAD.")
        return "{} {}".format(commit, branch)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def merge_config(remote: dict[str, Any], badge_version: str | None,
                 holder_name: str | None, write_git: bool = True) -> dict[str, Any]:
    config = load_local_defaults()
    for key in ("device_id", "holder_name", "badge_version", "git_commit"):
        if key in remote:
            config[key] = remote[key]
    if isinstance(remote.get("params"), dict):
        config["params"].update(remote["params"])
    if badge_version:
        config["badge_version"] = badge_version
    if holder_name is not None:
        config["holder_name"] = holder_name.strip()
    if write_git:
        config["git_commit"] = git_commit_info()
    return config


def should_upload(path: Path) -> bool:
    relative = path.relative_to(SOFTWARE_DIR)
    return (path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in relative.parts
            and path.suffix.lower() != ".pyc"
            and path.name.lower() not in SKIPPED_NAMES)


def upload_files() -> list[Path]:
    return sorted((path for path in SOFTWARE_DIR.rglob("*") if should_upload(path)),
                  key=lambda path: path.as_posix())


def clean_bytecode_cache() -> int:
    caches = list(SOFTWARE_DIR.rglob("__pycache__"))
    bytecode = [path for pattern in ("*.pyc", "*.pyo")
                for path in SOFTWARE_DIR.rglob(pattern)]
    for path in caches:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for path in bytecode:
        path.unlink(missing_ok=True)
    return len(caches) + len(bytecode)


def upload_entries(files: list[Path], root: Path = SOFTWARE_DIR) -> list[Path]:
    names = {path.relative_to(SOFTWARE_DIR).parts[0] for path in files}
    return [root / name for name in sorted(names)]


def stage_upload_files(files: list[Path], root: Path) -> list[Path]:
    """Copy approved files into an isolated tree for recursive upload."""
    for source in files:
        destination = root / source.relative_to(SOFTWARE_DIR)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return upload_entries(files, root)


def check_firmware_version(port: str | None, latest: Firmware) -> bool:
    code = (
        "import sys; v=sys.implementation.version; "
        "print('BADGE_MP_VERSION=%d.%d.%d' % (v[0],v[1],v[2]))"
    )
    result = run(mpremote_prefix(port) + ["exec", code], check=False,
                 capture=True, timeout=20)
    match = re.search(r"BADGE_MP_VERSION=(\d+\.\d+\.\d+)", result.stdout)
    if not match:
        print("Warning: could not read the badge's MicroPython version.")
        return False
    current = match.group(1)
    if current == latest.version:
        print("MicroPython {} is the latest stable release.".format(current))
        return True
    print("Warning: badge has MicroPython {}; latest stable is {}.".format(
        current, latest.version))
    return False


def maybe_check_firmware(port: str | None, skip: bool) -> None:
    if skip:
        return
    try:
        check_firmware_version(port, latest_firmware())
    except BadgeToolError as exc:
        print("Warning: {}".format(exc))


def write_remote_config(port: str | None, config: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="bsides-badge-") as temp_dir:
        local = Path(temp_dir) / "badge.json"
        local.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        run(mpremote_prefix(port) + ["fs", "cp", str(local), ":/badge.json"])


def battery_voltage_line(port: str | None,
                         config: dict[str, Any]) -> str | None:
    """Measure a 2026 badge now and return the final CLI output line."""
    if config.get("badge_version") != "2026":
        return None
    code = (
        "import battery; "
        "print('BADGE_BATTERY_VOLTAGE=%.3f' % "
        "battery.read_battery_voltage(4))"
    )
    result = run(mpremote_prefix(port) + ["exec", code], check=False,
                 capture=True, timeout=20)
    match = re.search(r"BADGE_BATTERY_VOLTAGE=(\d+(?:\.\d+)?)", result.stdout)
    if result.returncode != 0 or not match:
        return "Battery voltage: unavailable"
    voltage = float(match.group(1))
    line = "Battery voltage: {:.3f} V".format(voltage)
    if voltage < 3.8 or voltage > 4.2:
        line += " WARNING!! outside 3.8-4.2 V range"
    return line


def upload_tree(port: str | None, config: dict[str, Any]) -> str | None:
    removed = clean_bytecode_cache()
    if removed:
        print("Removed {} Python cache entries.".format(removed))
    files = upload_files()
    with tempfile.TemporaryDirectory(prefix="bsides-badge-upload-") as temp_dir:
        entries = stage_upload_files(files, Path(temp_dir))
        if entries:
            run(mpremote_prefix(port) + ["fs", "cp", "-r"]
                + [str(path) for path in entries] + [":"])
    write_remote_config(port, config)

    for stale_file in LEGACY_FILES + OBSOLETE_FILES:
        run(mpremote_prefix(port) + ["fs", "rm", ":/" + stale_file], check=False,
            capture=True, timeout=20)
    battery_line = battery_voltage_line(port, config)
    run(mpremote_prefix(port) + ["reset"], check=False, timeout=20)
    print("Uploaded {} firmware files and badge.json.".format(len(files)))
    return battery_line


def require_badge_version(value: str | None) -> str:
    if not value:
        raise BadgeToolError("--badge-version is required for this command.")
    return value


def command_init(args: argparse.Namespace) -> None:
    ensure_tools(install=True)
    firmware = latest_firmware()
    path = download_firmware(firmware, args.firmware_dir)
    print("Ready: MicroPython {} at {}".format(firmware.version, path))


def command_upload(args: argparse.Namespace) -> str | None:
    ensure_tools(install=False)
    version = require_badge_version(args.badge_version)
    port = detect_port(args.port)
    maybe_check_firmware(port, args.skip_version_check)
    remote = existing_config(port, args.wipe)
    config = merge_config(remote, version, args.holder_name, not args.no_git_info)
    return upload_tree(port, config)


def command_flash(args: argparse.Namespace) -> str | None:
    version = require_badge_version(args.badge_version)
    ensure_tools(install=True)
    firmware = latest_firmware()
    image = download_firmware(firmware, args.firmware_dir)
    port = detect_port(args.port)
    remote = existing_config(port, args.wipe)
    config = merge_config(remote, version, args.holder_name, not args.no_git_info)

    esptool = tool_command("esptool")
    if esptool is None:
        raise BadgeToolError("esptool is not installed or available on PATH.")
    port_args = ["--port", port] if port else []
    run(esptool + port_args + ["erase-flash"])
    run(esptool + port_args + ["--baud", str(args.baud), "write-flash", "0", str(image)])
    print("Waiting for MicroPython to start...")
    time.sleep(2)
    battery_line = upload_tree(port, config)
    print("Full flash complete with MicroPython {}.".format(firmware.version))
    return battery_line


def command_delete(args: argparse.Namespace) -> str | None:
    ensure_tools(install=False)
    port = detect_port(args.port)
    config = read_remote_config(port)
    battery_line = battery_voltage_line(port, config)
    code = """import os
def remove_tree(path):
 for entry in list(os.ilistdir(path)):
  child = path.rstrip('/') + '/' + entry[0]
  mode = entry[1] if len(entry) > 1 else os.stat(child)[0]
  if not mode:
   mode = os.stat(child)[0]
  if mode & 0x4000:
   remove_tree(child)
   os.rmdir(child)
  else:
   os.remove(child)
remove_tree('/')
print('BADGE_DELETE_OK')
"""
    result = run(mpremote_prefix(port) + ["exec", code], check=False,
                 capture=True, timeout=60)
    if result.returncode != 0 or "BADGE_DELETE_OK" not in result.stdout:
        message = (result.stderr or result.stdout).strip()
        raise BadgeToolError("Could not delete badge files: {}".format(message))
    print("Deleted all files from the badge filesystem; this cannot be undone.")
    return battery_line


def command_name(args: argparse.Namespace) -> str | None:
    ensure_tools(install=False)
    port = detect_port(args.port)
    maybe_check_firmware(port, args.skip_version_check)
    remote = read_remote_config(port)
    if not remote and not args.badge_version:
        raise BadgeToolError("Badge is not initialized; also pass --badge-version.")
    config = merge_config(remote, args.badge_version, args.name, not args.no_git_info)
    write_remote_config(port, config)
    battery_line = battery_voltage_line(port, config)
    run(mpremote_prefix(port) + ["reset"], check=False, timeout=20)
    print("Holder name updated.")
    return battery_line


def add_connection_options(parser: argparse.ArgumentParser, *, version: bool = False) -> None:
    parser.add_argument("--port", help="serial port (auto-detected by default)")
    if version:
        parser.add_argument("--badge-version", choices=SUPPORTED_BADGES, required=True)


def add_metadata_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--holder-name", help="also set the holder name")
    parser.add_argument("--no-git-info", action="store_true",
                        help="keep existing/default git info instead of writing HEAD")
    parser.add_argument(
        "--wipe", action="store_true",
        help="skip existing settings and create badge.json from defaults")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="install tools and download latest stable firmware")
    initialize.add_argument("--firmware-dir", type=Path, default=DEFAULT_FIRMWARE_DIR)
    initialize.set_defaults(handler=command_init)

    upload = subparsers.add_parser("upload", help="upload application files")
    add_connection_options(upload, version=True)
    add_metadata_options(upload)
    upload.add_argument("--skip-version-check", action="store_true")
    upload.set_defaults(handler=command_upload)

    flash = subparsers.add_parser("flash", help="erase, flash latest MicroPython, and upload files")
    add_connection_options(flash, version=True)
    add_metadata_options(flash)
    flash.add_argument("--firmware-dir", type=Path, default=DEFAULT_FIRMWARE_DIR)
    flash.add_argument("--baud", type=int, default=460800)
    flash.set_defaults(handler=command_flash)

    delete = subparsers.add_parser("delete", help="delete all files from the badge")
    add_connection_options(delete)
    delete.set_defaults(handler=command_delete)

    name = subparsers.add_parser("name", help="write the holder name to badge.json")
    add_connection_options(name)
    name.add_argument("name")
    name.add_argument("--badge-version", choices=SUPPORTED_BADGES)
    name.add_argument("--skip-version-check", action="store_true")
    name.add_argument("--no-git-info", action="store_true")
    name.set_defaults(handler=command_name)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        final_line = args.handler(args)
    except (BadgeToolError, OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    if final_line:
        print(final_line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
