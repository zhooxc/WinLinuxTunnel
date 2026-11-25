#!/usr/bin/env python3
"""Render the wlt app icon (rounded blue square + white bidirectional arrows)
and write win/wlt.ico (PNG-in-ICO, supported by Windows Vista+) and wlt.png.
Pure standard library. Run: python tools/make_icon.py"""
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S = 256


def render():
    buf = bytearray(S * S * 4)

    def put(x, y, rgba):
        if 0 <= x < S and 0 <= y < S:
            i = (y * S + x) * 4
            buf[i:i + 4] = bytes(rgba)

    # Vertical gradient blue rounded square.
    radius = 52
    top = (59, 130, 246)     # #3B82F6
    bot = (29, 78, 216)      # #1D4ED8
    for y in range(S):
        t = y / (S - 1)
        col = tuple(int(top[k] + (bot[k] - top[k]) * t) for k in range(3)) + (255,)
        for x in range(S):
            # rounded-rect coverage test
            cx = min(max(x, radius), S - 1 - radius)
            cy = min(max(y, radius), S - 1 - radius)
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= radius * radius:
                put(x, y, col)

    white = (255, 255, 255, 255)

    def rect(x0, y0, x1, y1):
        for y in range(y0, y1):
            for x in range(x0, x1):
                put(x, y, white)

    def tri(p0, p1, p2):
        xs = [p0[0], p1[0], p2[0]]
        ys = [p0[1], p1[1], p2[1]]
        for y in range(min(ys), max(ys) + 1):
            for x in range(min(xs), max(xs) + 1):
                d1 = (x - p1[0]) * (p0[1] - p1[1]) - (p0[0] - p1[0]) * (y - p1[1])
                d2 = (x - p2[0]) * (p1[1] - p2[1]) - (p1[0] - p2[0]) * (y - p2[1])
                d3 = (x - p0[0]) * (p2[1] - p0[1]) - (p2[0] - p0[0]) * (y - p0[1])
                neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
                pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
                if not (neg and pos):
                    put(x, y, white)

    # Top arrow pointing right.
    rect(56, 88, 170, 104)
    tri((168, 78), (168, 114), (204, 96))
    # Bottom arrow pointing left.
    rect(86, 152, 200, 168)
    tri((88, 142), (88, 178), (52, 160))
    return bytes(buf)


def png_bytes(rgba, w, h):
    rows = []
    for y in range(h):
        row = rgba[y * w * 4:(y + 1) * w * 4]
        rgb = bytearray(w * 3)
        rgb[0::3] = row[0::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[2::4]
        # preserve alpha? ICO PNG uses RGBA; keep 4 channels via color type 6
        rows.append(b"\x00" + row)
    payload = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # color type 6 = RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", payload) + chunk(b"IEND", b""))


def main():
    rgba = render()
    png = png_bytes(rgba, S, S)
    (ROOT / "win" / "wlt.png").write_bytes(png)
    # ICONDIR + ICONDIRENTRY + PNG (256 -> width/height byte 0)
    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    (ROOT / "win" / "wlt.ico").write_bytes(icondir + entry + png)
    print(f"wrote win/wlt.ico ({len(icondir + entry + png)} bytes) and win/wlt.png")


if __name__ == "__main__":
    main()