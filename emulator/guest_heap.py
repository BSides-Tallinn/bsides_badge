"""Copied only into instrumented emulator firmware."""
import gc
import uasyncio as asyncio


async def sample_heap():
    print("HEAP1 BOOT")
    sequence = 0
    while True:
        try:
            allocated = gc.mem_alloc()
            free = gc.mem_free()
            print("HEAP1 %d %d %d" % (sequence, allocated, free))
            sequence += 1
        except Exception as exc:
            print("Heap telemetry stopped:", exc)
            return
        await asyncio.sleep_ms(1000)
