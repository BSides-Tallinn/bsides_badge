"""Host orchestration for the badge emulator. Physical commands remain independent."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid

from . import fetch_dependencies as dependencies
from . import firmware_inputs

ROOT = Path(__file__).resolve().parents[1]
EMULATOR = ROOT / "emulator"
BUILD = ROOT / "firmware-build"
LOCK = EMULATOR / ".build.lock"
IMAGE = "bsides-firmware-builder:1.0"
FIRMWARE = "bsides-badge-esp32c3.bin"
ARTIFACTS = ("micropython.bin", "vfs.bin", FIRMWARE)
GENERATED = ("vfs.bin", FIRMWARE, "SHA256SUMS", "build-requirements.txt",
             "input.sha256", "badge-version", "build-profile.sha256", "guest-heap.json", "guest-heap.tmp")
BOARDS = {"2025": "board.json", "2025_prototype": "board-2025_prototype.json",
          "2026": "board-2026.json"}


class EmulatorError(RuntimeError):
    pass


def require_linux():
    if platform.system() != "Linux":
        raise EmulatorError("Emulator commands require Linux or WSL2")


@contextmanager
def locked(timeout=120):
    require_linux()
    import fcntl
    if not math.isfinite(timeout) or timeout < 0:
        raise EmulatorError("Lock timeout must be finite and nonnegative")
    deadline = time.monotonic() + timeout
    with LOCK.open("a+b") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EmulatorError("Build lock busy; stop the emulator or wait for the other operation")
                time.sleep(min(0.1, remaining))
        # Close, rather than explicitly unlocking: inherited descriptors
        # retain the lock until the last holder exits.
        yield stream.fileno()


def child(command, **kwargs):
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def manifest():
    return json.loads((EMULATOR / "dependencies.json").read_text(encoding="utf-8"))


def verify_dependencies():
    """Offline verification of pinned archives and installed runtime members."""
    data = manifest()
    esp = data["esp_emu"]
    native = esp["platforms"].get(platform.machine())
    if native is None:
        raise EmulatorError("Unsupported emulator host architecture")
    base = BUILD / "micropython.bin"
    items = ((base, data["micropython"]["sha256"]),
             (EMULATOR / ".cache" / native["archive"], native["sha256"]),
             (EMULATOR / ".cache" / esp["examples"]["archive"], esp["examples"]["sha256"]))
    for path, digest in items:
        if not path.is_file() or dependencies.sha256(path) != digest:
            raise EmulatorError(f"Missing or invalid dependency {path.name}; run emulator restore")
    for archive, name, installed in [
        (items[1][0], "esp-emu", EMULATOR / "bin" / "esp-emu"),
        *((items[2][0], name, EMULATOR / name) for name in esp["peripherals"]),
    ]:
        expected = hashlib.sha256(dependencies.member_bytes(archive, name)).hexdigest()
        if not installed.is_file() or dependencies.sha256(installed) != expected:
            raise EmulatorError(f"Missing or invalid dependency {name}; run emulator restore")
    if not os.access(EMULATOR / "bin" / "esp-emu", os.X_OK):
        raise EmulatorError("ESP-EMU is not executable; run emulator restore")


def verify_artifacts():
    """Require exactly the three expected checksum records; reject traversal."""
    try:
        records = {}
        for line in (BUILD / "SHA256SUMS").read_text(encoding="ascii").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not match or match[2] not in ARTIFACTS or match[2] in records:
                raise ValueError("Invalid artifact manifest entry")
            records[match[2]] = match[1]
        if set(records) != set(ARTIFACTS):
            raise ValueError("Incomplete artifact manifest")
        for name, digest in records.items():
            if dependencies.sha256(BUILD / name) != digest:
                raise ValueError(f"Checksum mismatch: {name}")
        if (BUILD / FIRMWARE).stat().st_size != 0x400000 or (BUILD / "vfs.bin").stat().st_size != 0x200000:
            raise ValueError("Unexpected firmware artifact size")
        if records["micropython.bin"] != manifest()["micropython"]["sha256"]:
            raise ValueError("Base firmware differs from dependency pin")
    except (OSError, ValueError) as exc:
        raise EmulatorError(f"Invalid firmware artifacts; run emulator build: {exc}") from exc


def profile():
    try:
        version, heap = (BUILD / "badge-version").read_text(encoding="ascii").strip().split(":")
        if version not in BOARDS or heap not in ("0", "1"):
            raise ValueError("Invalid profile")
        fingerprint = (BUILD / "input.sha256").read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Invalid fingerprint")
        if (BUILD / "build-profile.sha256").read_text(encoding="ascii").strip() != profile_digest(version, heap == "1", fingerprint):
            raise ValueError("Build profile checksum mismatch")
    except (OSError, ValueError) as exc:
        raise EmulatorError("Missing or invalid build metadata; run emulator build") from exc
    return version, heap == "1"


def profile_digest(version, heap, fingerprint):
    # Detect accidental profile edits or mixing metadata across builds.
    digest = hashlib.sha256(f"{version}:{int(heap)}:{fingerprint}\n".encode("ascii"))
    digest.update((BUILD / "SHA256SUMS").read_bytes())
    return digest.hexdigest()


def atomic_text(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(value, encoding="ascii")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def restore(args):
    with locked(args.lock_timeout) as fd:
        child([sys.executable, "-m", "emulator.fetch_dependencies"], pass_fds=(fd,))
        # Restore the builder too; build itself has no network access.
        (BUILD / "builder.identity").unlink(missing_ok=True)
        child(["docker", "build", "--provenance=false", "-t", IMAGE,
               "-f", "emulator/Dockerfile", "."], pass_fds=(fd,))
        image_id = child(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
                         capture_output=True, text=True).stdout.strip()
        atomic_text(BUILD / "builder.identity", builder_identity(image_id) + "\n")


def builder_identity(image_id):
    if not image_id:
        raise EmulatorError("Missing builder image identity")
    digest = hashlib.sha256(image_id.encode("ascii"))
    for name in ("Dockerfile", "requirements-build.lock", "requirements.lock"):
        digest.update((EMULATOR / name).read_bytes())
    return digest.hexdigest()


def build(args):
    with locked(args.lock_timeout) as fd:
        verify_dependencies()
        try:
            image_id = child(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
                             capture_output=True, text=True).stdout.strip()
            if (BUILD / "builder.identity").read_text(encoding="ascii").strip() != builder_identity(image_id):
                raise EmulatorError("Builder inputs changed; run emulator restore")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise EmulatorError("Builder unavailable; run emulator restore") from exc
        digest = firmware_inputs.fingerprint(args.badge_version, image_id, args.guest_heap)
        if not args.force:
            try:
                if profile() == (args.badge_version, args.guest_heap) and (BUILD / "input.sha256").read_text().strip() == digest:
                    verify_artifacts()
                    print("Firmware inputs unchanged")
                    return
            except (OSError, EmulatorError):
                pass
        for name in ("input.sha256", "badge-version", "build-profile.sha256"):
            (BUILD / name).unlink(missing_ok=True)
        # A Docker daemon container can outlive its CLI. Keep the host lock
        # while explicitly removing an interrupted build container.
        container_name = "badge-build-" + uuid.uuid4().hex
        try:
            child(["docker", "run", "--rm", "--name", container_name,
                   "--network", "none", "--pull", "never",
                   "--user", f"{os.getuid()}:{os.getgid()}",
                   "-e", f"BADGE_VERSION={args.badge_version}", "-e", f"GUEST_HEAP={int(args.guest_heap)}",
                   "-v", f"{ROOT}:/workspace", image_id], pass_fds=(fd,))
        except BaseException:
            # Best effort: the Docker daemon may be unavailable.
            subprocess.run(["docker", "rm", "-f", container_name], cwd=ROOT, check=False)
            raise
        verify_artifacts()
        if firmware_inputs.fingerprint(args.badge_version, image_id, args.guest_heap) != digest:
            raise EmulatorError("Firmware inputs changed during build; rebuild")
        atomic_text(BUILD / "badge-version", f"{args.badge_version}:{int(args.guest_heap)}\n")
        atomic_text(BUILD / "input.sha256", digest + "\n")
        atomic_text(BUILD / "build-profile.sha256", profile_digest(args.badge_version, args.guest_heap, digest) + "\n")


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def launch(command, heap, fd):
    """Supervise the emulator and optional console consumer without a shell."""
    processes = []
    try:
        emulator = subprocess.Popen(command, cwd=ROOT, pass_fds=(fd,),
                                    stdout=subprocess.PIPE if heap else None)
        processes.append(emulator)
        if not heap:
            code = emulator.wait()
            if code:
                raise subprocess.CalledProcessError(code, command)
            return
        try:
            capture = subprocess.Popen([sys.executable, "-m", "emulator.console_capture",
                                        str(BUILD / "guest-heap.json")],
                                       cwd=ROOT, stdin=emulator.stdout, pass_fds=(fd,))
            processes.append(capture)
        finally:
            emulator.stdout.close()
        while emulator.poll() is None and capture.poll() is None:
            time.sleep(0.1)
        if capture.poll() is not None and emulator.poll() is None:
            raise EmulatorError("Console capture exited while emulator was running")
        if emulator.returncode:
            raise subprocess.CalledProcessError(emulator.returncode, command)
        code = capture.wait(timeout=5)
        if code:
            raise subprocess.CalledProcessError(code, capture.args)
    finally:
        try:
            if processes:
                stop_process(processes[0])
        finally:
            if len(processes) > 1:
                stop_process(processes[1])


def run(args):
    with locked(args.lock_timeout) as fd:
        verify_artifacts()
        version, heap = profile()
        verify_dependencies()
        board = EMULATOR / BOARDS[version]
        if not board.is_file():
            raise EmulatorError(f"Missing board definition: {board.name}")
        for name in ("guest-heap.json", "guest-heap.tmp"):
            (BUILD / name).unlink(missing_ok=True)
        command = [str(EMULATOR / "bin" / "esp-emu"), "--chip", "esp32c3",
                   "--firmware", str(BUILD / FIRMWARE), "--cosim-board", str(board),
                   "--control-tcp", f"127.0.0.1:{args.port}"]
        if args.cosim_log:
            command += ["--cosim-log", str(EMULATOR / "cosim.jsonl")]
        launch(command, heap, fd)


def client(args):
    require_linux()
    module = "emulator.controller" if args.emulator_command == "controller" else "emulator.measure_rgb_fps"
    command = [sys.executable, "-m", module, "--host", args.host, "--port", str(args.port)]
    if args.guest_heap:
        command += ["--heap-file", str(BUILD / "guest-heap.json")]
    if args.emulator_command == "controller":
        command += ["--refresh-hz", str(args.refresh_hz), "--led-colors", args.led_colors,
                    "--led-offset", str(args.led_offset), "--press-ms", str(args.press_ms)]
        if args.led_counterclockwise:
            command.append("--led-counterclockwise")
        if args.emulator_pid is not None:
            command += ["--emulator-pid", str(args.emulator_pid)]
        for name in ("oled", "rgb", "back", "prev", "next", "select"):
            command += ["--" + name, getattr(args, name)]
    else:
        command += ["--duration", str(args.duration), "--interval", str(args.interval),
                    "--expected-pixels", str(args.expected_pixels)]
        if args.memory:
            command.append("--memory")
    child(command)


def clean(args):
    with locked(args.lock_timeout):
        if args.all:
            for directory in (BUILD, EMULATOR / "bin", EMULATOR / ".cache"):
                if directory.exists():
                    shutil.rmtree(directory)
            for name in manifest()["esp_emu"]["peripherals"]:
                (EMULATOR / name).unlink(missing_ok=True)
        else:
            for name in GENERATED:
                (BUILD / name).unlink(missing_ok=True)
        for name in ("cosim.jsonl", "oled.png"):
            (EMULATOR / name).unlink(missing_ok=True)


def tests(args):
    suites = ["tests"] if args.app else ["emulator/tests/unit"] if args.emulator else ["emulator/tests/unit", "tests"]
    failed = False
    for suite in suites:
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", suite, "-v"], cwd=ROOT)
        failed |= result.returncode != 0
    if failed:
        raise EmulatorError("Regression tests failed")


def add_commands(subparsers):
    test = subparsers.add_parser("test", help="run regression tests")
    group = test.add_mutually_exclusive_group()
    group.add_argument("--app", action="store_true")
    group.add_argument("--emulator", action="store_true")
    test.set_defaults(handler=tests)
    emulator = subparsers.add_parser("emulator", help="manage virtual badges")
    commands = emulator.add_subparsers(dest="emulator_command", required=True)
    for name, handler, help_text in (
        ("restore", restore, "restore pinned dependencies and builder"),
        ("build", build, "build or reuse firmware"),
        ("run", run, "launch existing verified firmware"),
        ("controller", client, "launch interactive controller"),
        ("benchmark", client, "measure RGB throughput"),
        ("clean", clean, "remove generated artifacts"),
    ):
        parser = commands.add_parser(name, help=help_text)
        parser.set_defaults(handler=handler)
        if name in ("restore", "build", "run", "clean"):
            parser.add_argument("--lock-timeout", type=float, default=120)
        if name == "build":
            parser.add_argument("--badge-version", choices=BOARDS, default="2025")
            parser.add_argument("--guest-heap", action="store_true")
            parser.add_argument("--force", action="store_true")
        if name in ("run", "controller", "benchmark"):
            parser.add_argument("--port", type=port_number, default=5556)
        if name == "run":
            parser.add_argument("--cosim-log", action="store_true")
        if name in ("controller", "benchmark"):
            parser.add_argument("--host", default="127.0.0.1")
            parser.add_argument("--guest-heap", action="store_true")
        if name == "controller":
            parser.add_argument("--refresh-hz", type=float, default=75)
            parser.add_argument("--led-colors", choices=("visible", "raw"), default="visible")
            parser.add_argument("--led-offset", type=int, default=0)
            parser.add_argument("--led-counterclockwise", action="store_true")
            parser.add_argument("--press-ms", type=int, default=150)
            parser.add_argument("--emulator-pid", type=int)
            for part in ("oled", "rgb", "back", "prev", "next", "select"):
                parser.add_argument("--" + part, default=part)
        if name == "benchmark":
            parser.add_argument("--duration", type=float, default=30)
            parser.add_argument("--interval", type=float, default=0.01)
            parser.add_argument("--expected-pixels", type=int, default=16)
            parser.add_argument("--memory", action="store_true")
        if name == "clean":
            parser.add_argument("--all", action="store_true")


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535")
    return number
