import struct
import tempfile
import unittest
from pathlib import Path
from emulator.validate_firmware import validate_base, validate_merged


def base_image():
    data = bytearray(0x10000 + 24)
    data[0] = 0xe9
    data[3] = 0x20
    struct.pack_into("<H", data, 12, 5)
    for i, (label, typ, sub, off, size) in enumerate([
        (b"nvs", 1, 2, 0x9000, 0x6000),
        (b"phy_init", 1, 1, 0xf000, 0x1000),
        (b"factory", 0, 0, 0x10000, 0x1f0000),
    ]):
        struct.pack_into("<HBBII16sI", data, 0x8000 + 32*i,
                         0x50aa, typ, sub, off, size, label, 0)
    struct.pack_into("<H", data, 0x8060, 0xffff)
    data[0x10000:0x10002] = b"\xe9\x01"
    return bytes(data)


class ValidationTests(unittest.TestCase):
    def test_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "base.bin"
            data = base_image()
            path.write_bytes(data)
            validate_base(path)
            for invalid in (data[:0x10000], data[:0x10000]+bytes(24),
                            data[:12]+b"\0"+data[13:]):
                path.write_bytes(invalid)
                with self.assertRaises(ValueError):
                    validate_base(path)

    def test_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            base = path/"base.bin"
            base.write_bytes(base_image())
            vfs = b"x"*0x200000
            (path/"vfs.bin").write_bytes(vfs)
            image = base_image().ljust(0x200000,b"\xff")+vfs
            merged = path/"merged.bin"
            merged.write_bytes(image)
            validate_merged(path, base)
            for offset in (0, 0x10000, 0x1fffff, 0x200000):
                invalid = bytearray(image)
                invalid[offset] ^= 1
                merged.write_bytes(invalid)
                with self.subTest(offset=offset), self.assertRaises(ValueError):
                    validate_merged(path, base)
