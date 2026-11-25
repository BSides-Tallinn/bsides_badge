#!/usr/bin/env python3
"""Restore versioned emulator dependencies without global installation."""
import hashlib
from http.client import HTTPException
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
EMULATOR = ROOT / "emulator"
BUILD = ROOT / "firmware-build"
CACHE = EMULATOR / ".cache"
BIN = EMULATOR / "bin"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, destination, expected):
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Invalid SHA-256 digest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256(destination) == expected:
        print(f"Verified: {destination.name}")
        return destination
    print(f"Downloading: {url}")
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
    try:
        with urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        actual = sha256(temporary)
        if actual != expected:
            raise ValueError(f"Checksum mismatch for {destination.name}: expected {expected}, got {actual}")
        print(f"SHA-256: {actual}")
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def member_bytes(archive, name):
    with tarfile.open(archive, "r:gz") as source:
        matches = [m for m in source.getmembers() if m.isfile() and Path(m.name).name == name]
        if not matches:
            raise ValueError(f"{name} not found in {archive.name}")
        contents = []
        for member in matches:
            with source.extractfile(member) as stream:
                contents.append(stream.read())
        if any(data != contents[0] for data in contents[1:]):
            raise ValueError(f"Ambiguous archive members: {name}")
    return contents[0]


def install_member(archive, name, destination, mode=0o644):
    data = member_bytes(archive, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256(destination) == hashlib.sha256(data).hexdigest():
        destination.chmod(mode)
        return
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
    try:
        temporary.write_bytes(data)
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def setup():
    manifest = json.loads((EMULATOR / "dependencies.json").read_text())
    mp = manifest["micropython"]
    esp = manifest["esp_emu"]
    if platform.system() != "Linux" or platform.machine() not in esp["platforms"]:
        raise RuntimeError("Supported hosts: Linux/WSL2 x86_64 and aarch64")
    native = esp["platforms"][platform.machine()]
    examples = esp["examples"]
    download(mp["url"], BUILD / "micropython.bin", mp["sha256"])
    archives = [
        download(f"{esp['release_url']}/{item['archive']}", CACHE / item["archive"], item["sha256"])
        for item in (native, examples)
    ]
    install_member(archives[0], "esp-emu", BIN / "esp-emu", 0o755)
    for name in esp["peripherals"]:
        install_member(archives[1], name, EMULATOR / name)
    print("Emulator dependencies ready.")


def main():
    try:
        setup()
    except (OSError, ValueError, RuntimeError, tarfile.TarError, HTTPException, KeyError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
