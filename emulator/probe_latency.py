"""Compare sequential and batched control RTT with TCP_NODELAY on/off."""
import argparse
import socket
import statistics
import time
if __package__:
    from .esp_control import EspControl, ProtocolError
else:
    from esp_control import EspControl, ProtocolError


def execute(control, commands, batched):
    if batched:
        for reply in control.commands(commands):
            if isinstance(reply, ProtocolError):
                raise reply
    else:
        for command in commands:
            control.command(command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5556)
    parser.add_argument("--samples", type=int, default=50)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("At least two samples")
    with EspControl(args.host, args.port) as control:
        for label, commands, batched in (
            ("ping", ("ping",), False),
            ("rgb", ("part rgb state",), False),
            ("oled_state", ("part oled state",), False),
            ("sequential_pair", ("part oled render", "part rgb state"), False),
            ("batched_pair", ("part oled render", "part rgb state"), True),
        ):
            times = {False: [], True: []}
            for mode in (False, True):
                control.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, int(mode))
                for _ in range(3):
                    execute(control, commands, batched)
            for i in range(args.samples):
                for mode in ((False, True) if i % 2 == 0 else (True, False)):
                    control.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, int(mode))
                    before = time.monotonic()
                    execute(control, commands, batched)
                    times[mode].append((time.monotonic()-before)*1000)
            for mode, values in times.items():
                print(f"nodelay={mode} {label}: median={statistics.median(values):.2f} ms min={min(values):.2f} max={max(values):.2f}")


if __name__ == "__main__":
    main()
