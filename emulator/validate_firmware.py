"""Validate the fixed ESP32-C3 flash layout and generated image."""
import argparse
import struct
from pathlib import Path


def validate_base(path):
    b = path.read_bytes()
    if len(b) > 0x200000 or len(b) < 0x10000 + 24 or b[0] != 0xe9:
        raise ValueError('Unexpected base firmware size or header')
    if struct.unpack_from('<H', b, 12)[0] != 5 or b[3] >> 4 != 2:
        raise ValueError('Expected ESP32-C3 with 4 MiB flash')
    if b[0x10000] != 0xe9 or b[0x10001] == 0:
        raise ValueError('Missing factory application image header')
    parts = []
    for pos in range(0x8000, 0x8c00, 32):
        magic, typ, sub, off, size, label, flags = struct.unpack_from('<HBBII16sI', b, pos)
        if magic in (0xffff, 0xebeb):
            break
        if magic != 0x50aa:
            raise ValueError('Invalid partition table')
        parts.append((label.rstrip(b'\0'), typ, sub, off, size, flags))
    expected = [(b'nvs',1,2,0x9000,0x6000,0),(b'phy_init',1,1,0xf000,0x1000,0),(b'factory',0,0,0x10000,0x1f0000,0)]
    if parts != expected:
        raise ValueError(f'Unexpected partition table: {parts!r}')


def validate_merged(path, base):
    validate_base(base)
    expected = base.read_bytes().ljust(0x200000, b'\xff')
    image = (path / 'merged.bin').read_bytes()
    vfs = (path / 'vfs.bin').read_bytes()
    if len(image) != 0x400000 or len(vfs) != 0x200000 or image[:0x200000] != expected or image[0x200000:] != vfs:
        raise ValueError('Merged image validation failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('base', 'merged'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--base', type=Path)
    args = parser.parse_args()
    if args.kind == 'base':
        validate_base(args.path)
    else:
        if args.base is None:
            parser.error('--base is required for merged validation')
        validate_merged(args.path, args.base)
