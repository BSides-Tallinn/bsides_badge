"""Generate a hash-locked requirements file from exact, reviewed package pins.

Use PyPI release metadata rather than resolving newer dependency versions.
Include wheels and source distributions for each non-yanked release.
The resulting lock must be reviewed and tested before committing.
"""
import json
import re
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen


HERE = Path(__file__).resolve().parent
PIN = re.compile(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+-]+)")
SHA256 = re.compile(r"[0-9a-f]{64}")


def generate(pins, fetch=urlopen):
    lines = ["# Generated from requirements.pins by generate_lock.py.",
             "# Review changes before committing.", ""]
    seen = set()
    for line in pins.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"Expected exact package pin: {line}")
        name, version = match.groups()
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if canonical in seen:
            raise ValueError(f"Duplicate package: {name}")
        seen.add(canonical)
        url = f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json"
        with fetch(url, timeout=30) as response:
            metadata = json.load(response)
        info = metadata["info"]
        if re.sub(r"[-_.]+", "-", info["name"]).lower() != canonical or info["version"] != version:
            raise ValueError(f"Unexpected release metadata: {line}")
        hashes = set()
        for artifact in metadata["urls"]:
            if artifact.get("yanked") or artifact.get("packagetype") not in ("bdist_wheel", "sdist"):
                continue
            digest = artifact["digests"]["sha256"]
            if not SHA256.fullmatch(digest):
                raise ValueError(f"Invalid SHA-256: {line}")
            hashes.add(digest)
        if not hashes:
            raise ValueError(f"No usable release artifacts: {line}")
        lines.append(line + " " + " ".join("--hash=sha256:" + h for h in sorted(hashes)))
    if not seen:
        raise ValueError("No package pins")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    # Generate fully before printing; make lock preserves the old file via a temporary file.
    result = generate((HERE / "requirements.pins").read_text(encoding="utf-8"))
    print(result, end="")
