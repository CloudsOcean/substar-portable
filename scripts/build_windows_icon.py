from __future__ import annotations

import argparse
import binascii
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = ROOT / "assets" / "substar.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _bezier(start, control1, control2, end, steps=64):
    for index in range(steps):
        t = index / steps
        u = 1.0 - t
        yield (
            u**3 * start[0] + 3 * u * u * t * control1[0] + 3 * u * t * t * control2[0] + t**3 * end[0],
            u**3 * start[1] + 3 * u * u * t * control1[1] + 3 * u * t * t * control2[1] + t**3 * end[1],
        )


def _brand_outline():
    segments = (
        ((32, 4), (35.02, 20.35), (43.65, 28.98), (60, 32)),
        ((60, 32), (43.65, 35.02), (35.02, 43.65), (32, 60)),
        ((32, 60), (28.98, 43.65), (20.35, 35.02), (4, 32)),
        ((4, 32), (20.35, 28.98), (28.98, 20.35), (32, 4)),
    )
    return [point for segment in segments for point in _bezier(*segment)]


def _rgba(size: int) -> bytes:
    scale = size * 4 / 64
    points = [(x * scale, y * scale) for x, y in _brand_outline()]
    width = size * 4
    coverage = [0] * (size * size)
    edges = list(zip(points, points[1:] + points[:1]))
    for sample_y in range(width):
        y = sample_y + 0.5
        intersections = []
        for (x1, y1), (x2, y2) in edges:
            if (y1 <= y < y2) or (y2 <= y < y1):
                intersections.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        intersections.sort()
        for left, right in zip(intersections[0::2], intersections[1::2]):
            start = max(0, int(left + 0.5))
            stop = min(width, int(right + 0.5))
            row = (sample_y // 4) * size
            for sample_x in range(start, stop):
                coverage[row + sample_x // 4] += 1
    pixels = bytearray()
    for count in coverage:
        alpha = min(255, round(count * 255 / 16))
        pixels.extend((169, 113, 255, alpha))
    return bytes(pixels)


def _png(size: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)

    rgba = _rgba(size)
    rows = b"".join(b"\0" + rgba[row * size * 4 : (row + 1) * size * 4] for row in range(size))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def build_icon(path: Path = ICON_PATH) -> None:
    images = [(size, _png(size)) for size in SIZES]
    offset = 6 + 16 * len(images)
    entries = []
    payload = bytearray()
    for size, image in images:
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(image), offset))
        payload.extend(image)
        offset += len(image)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + payload)


def check_icon(path: Path = ICON_PATH) -> None:
    data = path.read_bytes()
    if len(data) < 6 or struct.unpack("<HHH", data[:6]) != (0, 1, len(SIZES)):
        raise SystemExit("Substar ICO is missing or invalid")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    check_icon() if args.check else build_icon()
