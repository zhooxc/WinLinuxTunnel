#!/usr/bin/env python3
"""Render the WinLinuxTunnel app icon as a MULTI-RESOLUTION .ico.

The taskbar/titlebar use small icons (16/24/32/48px). A single 256px image
downscaled by Windows looks blurry; instead we render EVERY size natively
with supersampled anti-aliasing, so Windows picks an exact, crisp match.

Design: rounded blue gradient square + white bidirectional arrows.
Output: win/wlt.ico (16/24/32/48/64/128/256) and win/wlt.png (256 preview).
Pure standard library. Run: python tools/make_icon.py
"""
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Normalized geometry (fractions of the icon side).
R = 0.20            # corner radius
TH = 0.03125        # arrow shaft half-thickness
# top arrow (points right)
T_CY = 0.375
T_X0, T_X1 = 0.22, 0.66
T_HEAD = ((0.656, 0.305), (0.656, 0.445), (0.80, 0.375))
# bottom arrow (points left)
B_CY = 0.625
B_X0, B_X1 = 0.34, 0.78
B_HEAD = ((0.344, 0.555), (0.344, 0.695), (0.20, 0.625))


def _in_tri(px, py, tri):
    (x0, y0), (x1, y1), (x2, y2) = tri
    d1 = (px - x1) * (y0 - y1) - (x0 - x1) * (py - y1)
    d2 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d3 = (px - x0) * (y2 - y0) - (x2 - x0) * (py - y0)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def _in_arrows(nx, ny):
    if T_X0 <= nx <= T_X1 and abs(ny - T_CY) <= TH:
        return True
    if B_X0 <= nx <= B_X1 and abs(ny - B_CY) <= TH:
        return True
    return _in_tri(nx, ny, T_HEAD) or _in_tri(nx, ny, B_HEAD)


def _sample(nx, ny):
    """RGBA at normalized point (nx, ny) in [0,1]."""
    cx = min(max(nx, R), 1 - R)
    cy = min(max(ny, R), 1 - R)
    dx, dy = nx - cx, ny - cy
    if dx * dx + dy * dy > R * R:
        return (0, 0, 0, 0)
    if _in_arrows(nx, ny):
        return (255, 255, 255, 255)
    t = ny
    return (int(59 + (29 - 59) * t), int(130 + (78 - 130) * t),
            int(246 + (216 - 246) * t), 255)


def render(S):
    """Render S x S RGBA with supersampled anti-aliasing."""
    F = 4 if S <= 64 else 2          # supersample factor
    SS = S * F
    # coverage buffer at SS resolution (RGBA)
    big = bytearray(SS * SS * 4)
    for y in range(SS):
        ny = (y + 0.5) / SS
        row = y * SS * 4
        for x in range(SS):
            nx = (x + 0.5) / SS
            r, g, b, a = _sample(nx, ny)
            i = row + x * 4
            big[i] = r; big[i + 1] = g; big[i + 2] = b; big[i + 3] = a
    # box downscale by F -> anti-aliased S x S
    out = bytearray(S * S * 4)
    n = F * F
    for y in range(S):
        for x in range(S):
            sr = sg = sb = sa = 0
            for dy in range(F):
                base = ((y * F + dy) * SS + x * F) * 4
                for dx in range(F):
                    i = base + dx * 4
                    sr += big[i]; sg += big[i + 1]; sb += big[i + 2]; sa += big[i + 3]
            o = (y * S + x) * 4
            out[o] = sr // n; out[o + 1] = sg // n; out[o + 2] = sb // n; out[o + 3] = sa // n
    return bytes(out)


def png_bytes(rgba, w, h):
    rows = [b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h)]
    payload = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", payload) + chunk(b"IEND", b"")


def main():
    images = []
    for S in SIZES:
        rgba = render(S)
        png = png_bytes(rgba, S, S)
        images.append((S, png))
        print(f"  rendered {S}x{S} ({len(png)} bytes)")
    # ICO with multiple entries (smallest first).
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    blob = b""
    for S, png in images:
        wh = 0 if S >= 256 else S
        entries += struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(png), offset)
        blob += png
        offset += len(png)
    ico = header + entries + blob
    (ROOT / "win" / "wlt.ico").write_bytes(ico)
    (ROOT / "win" / "wlt.png").write_bytes(images[-1][1])
    print(f"wrote win/wlt.ico ({len(ico)} bytes, {count} sizes) + win/wlt.png")


if __name__ == "__main__":
    main()