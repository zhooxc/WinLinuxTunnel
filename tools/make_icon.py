#!/usr/bin/env python3
"""Render the WinLinuxTunnel app icon as a MULTI-RESOLUTION .ico.

The taskbar / tray use small icons (16/24/32/48px). A single large image
downscaled by Windows looks blurry, so every size is drawn natively with
supersampled anti-aliasing and size-adaptive geometry (thicker strokes at
small sizes so the silhouette stays readable).

Design: soft azure rounded square + bold white bidirectional arrows
(Windows ↔ Linux tunnel). Matches the GUI accent #2563eb.

Output: win/wlt.ico (16/24/32/48/64/128/256) and win/wlt.png (256 preview).
Pure standard library. Run: python tools/make_icon.py
"""
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Corner radius as a fraction of side (Windows-friendly squircle).
R = 0.22


def _lerp(a, b, t):
    return a + (b - a) * t


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _smoothstep(edge0, edge1, x):
    t = _clamp((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def _in_rounded_rect(nx, ny, r):
    cx = min(max(nx, r), 1.0 - r)
    cy = min(max(ny, r), 1.0 - r)
    dx, dy = nx - cx, ny - cy
    return dx * dx + dy * dy <= r * r


def _in_tri(px, py, tri):
    (x0, y0), (x1, y1), (x2, y2) = tri
    d1 = (px - x1) * (y0 - y1) - (x0 - x1) * (py - y1)
    d2 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d3 = (px - x0) * (y2 - y0) - (x2 - x0) * (py - y0)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def _geom(S):
    """Size-adaptive arrow geometry in normalized [0,1] coords.

    Tray sizes keep a blue gap between arrows and a wide, tall head so the
    tip reads as a triangle even at 16px. Large sizes get rounded shafts.
    """
    px = 1.0 / S
    # Shaft half-thickness in px. Aim ~1.5–2px at 16 so heads still fit.
    th_px = 1.5 if S <= 16 else 2.0 if S <= 24 else 2.5 if S <= 48 else max(3.5, 0.055 * S)
    th = th_px * px
    # Gap between shaft outer edges; must exceed head overshoot so heads
    # don't collide: head_h = th * 1.85 ⇒ need clear > 1.7*th.
    clear_px = 3.0 if S <= 16 else 3.0 if S <= 32 else 4.0 if S <= 64 else 0.06 * S
    center_sep = 2 * th + clear_px * px
    head_w = 0.20 if S <= 32 else 0.17             # wide tip at tray sizes
    head_h = th * 1.85
    pad = 0.16 if S >= 48 else 0.125
    round_caps = S >= 48

    mid = 0.50
    t_cy = mid - center_sep / 2.0
    b_cy = mid + center_sep / 2.0

    # Top arrow → right: shaft stops where the triangle base begins.
    t_x0 = pad
    t_x1 = 1.0 - pad - head_w
    t_tip = 1.0 - pad
    t_head = (
        (t_x1, t_cy - head_h),
        (t_x1, t_cy + head_h),
        (t_tip, t_cy),
    )

    # Bottom arrow ← left
    b_x1 = 1.0 - pad
    b_x0 = pad + head_w
    b_tip = pad
    b_head = (
        (b_x0, b_cy - head_h),
        (b_x0, b_cy + head_h),
        (b_tip, b_cy),
    )

    return {
        "th": th, "round_caps": round_caps,
        "t_cy": t_cy, "t_x0": t_x0, "t_x1": t_x1, "t_head": t_head,
        "b_cy": b_cy, "b_x0": b_x0, "b_x1": b_x1, "b_head": b_head,
    }


def _in_shaft(nx, ny, x0, x1, cy, th, round_caps):
    """Horizontal shaft; rounded tail only on large icons."""
    if x0 <= nx <= x1 and abs(ny - cy) <= th:
        return True
    if not round_caps:
        return False
    # Round the tail end only (away from the arrowhead).
    # Caller passes x0 as tail for → arrows and x1 as tail for ← arrows
    # via the dedicated tail_x argument pattern below — here we round both
    # ends lightly; heads cover the tip side.
    if (nx - x0) ** 2 + (ny - cy) ** 2 <= th * th:
        return True
    if (nx - x1) ** 2 + (ny - cy) ** 2 <= th * th:
        return True
    return False


def _in_arrows(nx, ny, g):
    th = g["th"]
    rc = g["round_caps"]
    if _in_shaft(nx, ny, g["t_x0"], g["t_x1"], g["t_cy"], th, rc):
        return True
    if _in_shaft(nx, ny, g["b_x0"], g["b_x1"], g["b_cy"], th, rc):
        return True
    return _in_tri(nx, ny, g["t_head"]) or _in_tri(nx, ny, g["b_head"])


def _bg_rgba(nx, ny, S):
    """Azure gradient matching GUI accent #2563eb + light top sheen."""
    # #1e3a8a → #2563eb → #60a5fa  (stay in brand blue, avoid cyan)
    t = _smoothstep(0.04, 0.96, ny)
    if t < 0.50:
        u = t / 0.50
        r = int(_lerp(30, 37, u))
        g = int(_lerp(58, 99, u))
        b = int(_lerp(138, 235, u))
    else:
        u = (t - 0.50) / 0.50
        r = int(_lerp(37, 96, u))
        g = int(_lerp(99, 165, u))
        b = int(_lerp(235, 250, u))

    # Soft top sheen (modern, not plastic). Skip below 32px — wastes contrast.
    if S >= 32:
        sheen = (1.0 - _smoothstep(0.06, 0.40, ny)) * 0.14
        sheen *= 0.70 + 0.30 * math.sin(math.pi * _clamp(nx))
        r = int(_clamp(r + 200 * sheen, 0, 255))
        g = int(_clamp(g + 210 * sheen, 0, 255))
        b = int(_clamp(b + 230 * sheen, 0, 255))

    return (r, g, b, 255)


def _sample(nx, ny, S, g):
    if not _in_rounded_rect(nx, ny, R):
        return (0, 0, 0, 0)
    if _in_arrows(nx, ny, g):
        return (255, 255, 255, 255)
    return _bg_rgba(nx, ny, S)


def render(S):
    """Render S x S RGBA with supersampled anti-aliasing."""
    F = 8 if S <= 32 else 4 if S <= 128 else 3
    SS = S * F
    g = _geom(S)
    big = bytearray(SS * SS * 4)
    for y in range(SS):
        ny = (y + 0.5) / SS
        row = y * SS * 4
        for x in range(SS):
            nx = (x + 0.5) / SS
            r, gv, b, a = _sample(nx, ny, S, g)
            i = row + x * 4
            big[i] = r
            big[i + 1] = gv
            big[i + 2] = b
            big[i + 3] = a
    out = bytearray(S * S * 4)
    n = F * F
    for y in range(S):
        for x in range(S):
            sr = sg = sb = sa = 0
            for dy in range(F):
                base = ((y * F + dy) * SS + x * F) * 4
                for dx in range(F):
                    i = base + dx * 4
                    sr += big[i]
                    sg += big[i + 1]
                    sb += big[i + 2]
                    sa += big[i + 3]
            o = (y * S + x) * 4
            out[o] = sr // n
            out[o + 1] = sg // n
            out[o + 2] = sb // n
            out[o + 3] = sa // n
    return bytes(out)


def png_bytes(rgba, w, h):
    rows = [b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h)]
    payload = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", payload)
            + chunk(b"IEND", b""))


def main():
    images = []
    for S in SIZES:
        rgba = render(S)
        png = png_bytes(rgba, S, S)
        images.append((S, png))
        print(f"  rendered {S}x{S} ({len(png)} bytes)")
    # ICO with multiple PNG entries (smallest first).
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
