"""Stage the application and fingerprint the exact firmware build inputs."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ("2025", "2025_prototype", "2026")


def source_files(root):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink not supported: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Unsupported source entry: {path}")
        if path.name in ("badge.json", "params.json", "id.txt", "yourname.txt") or path.suffix in (".log", ".pyc", ".pyo"):
            continue
        if path.suffix not in (".py", ".pem", ".json"):
            raise ValueError(f"Unknown firmware asset: {relative}; update staging policy explicitly")
        yield relative, path


def staged_config(version):
    # Runtime defaults are defined by badge_config.py. Do not include local identity.
    if version not in VERSIONS:
        raise ValueError("Unsupported badge revision")
    return (json.dumps({"badge_version": version, "device_id": "", "holder_name": "",
                        "git_commit": "unknown"}, sort_keys=True) + "\n").encode()


def stage(source, destination, version, guest_heap=False):
    for relative, path in source_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    (destination / "badge.json").write_bytes(staged_config(version))
    if guest_heap:
        target = destination / "bsides.py"
        text = target.read_text(encoding="utf-8")
        anchor = "    await asyncio.gather(*tasks)"
        if text.count(anchor) != 1:
            raise ValueError("Guest telemetry insertion point changed")
        target.write_text(text.replace(anchor, "    from guest_heap import sample_heap\n    tasks.append(sample_heap())\n" + anchor), encoding="utf-8")
        shutil.copyfile(ROOT / "emulator/guest_heap.py", destination / "guest_heap.py")


def fingerprint(version, image_id, guest_heap=False):
    digest = hashlib.sha256()
    def add(name, data):
        digest.update(name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    add("version", staged_config(version))
    add("builder", image_id.encode())
    add("guest_heap", str(guest_heap).encode())
    if guest_heap:
        add("emulator/guest_heap.py", (ROOT / "emulator/guest_heap.py").read_bytes())
    for relative, path in source_files(ROOT / "software"):
        add(str(relative), path.read_bytes())
    for name in ("emulator/Dockerfile", "emulator/build_firmware.sh", "emulator/firmware_inputs.py",
                 "emulator/dependencies.json", "emulator/requirements.lock", "emulator/requirements-build.lock", "emulator/validate_firmware.py", "firmware-build/micropython.bin"):
        add(name, (ROOT / name).read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    staging = sub.add_parser("stage")
    staging.add_argument("destination", type=Path)
    staging.add_argument("version", choices=VERSIONS)
    hashing = sub.add_parser("fingerprint")
    hashing.add_argument("version", choices=VERSIONS)
    hashing.add_argument("image_id")
    staging.add_argument("--guest-heap", action="store_true")
    hashing.add_argument("--guest-heap", action="store_true")
    args = parser.parse_args()
    if args.command == "stage":
        stage(ROOT / "software", args.destination, args.version, args.guest_heap)
    else:
        print(fingerprint(args.version, args.image_id, args.guest_heap))


if __name__ == "__main__":
    main()
