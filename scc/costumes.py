# SPDX-License-Identifier: AGPL-3.0-only
"""
Costume geometry (and, later, rasterization).

Logical costume size drives sprite bounds: fencing, bouncing, touching and
`set size` clamping all depend on it, exactly as in scratch-render.
"""
import re
import struct


def svg_size(data):
    """Width/height of an SVG as scratch-render sees it: the root width/height
    attributes, else the viewBox."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return 0.0, 0.0
    m = re.search(r"<svg\b[^>]*>", text, re.S)
    if not m:
        return 0.0, 0.0
    tag = m.group(0)

    def attr(name):
        a = re.search(r'\s%s\s*=\s*["\']\s*([-\d.eE+]+)' % name, tag)
        return float(a.group(1)) if a else None
    w, h = attr("width"), attr("height")
    if w is None or h is None:
        vb = re.search(r'viewBox\s*=\s*["\']\s*([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)', tag)
        if vb:
            w = w if w is not None else float(vb.group(3))
            h = h if h is not None else float(vb.group(4))
    return (w or 0.0), (h or 0.0)


def bitmap_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return float(w), float(h)
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return float(w), float(h)
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
    return 0.0, 0.0


def measure(costume):
    if costume.asset is None:
        costume.w = costume.h = 0.0
        return
    if costume.data_format == "svg":
        costume.w, costume.h = svg_size(costume.asset)
    else:
        w, h = bitmap_size(costume.asset)
        costume.w, costume.h = w / costume.bitmap_res, h / costume.bitmap_res


def measure_all(prog):
    for s in prog.sprites:
        for c in s.costumes:
            measure(c)


# ---------------------------------------------------------------------------
# Rasterization (for the software renderer)

import zlib


def decode_png(data):
    """Decode a non-interlaced 8-bit PNG to (w, h, RGBA bytes). Returns None
    for formats this small decoder does not handle."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    idat = b""
    w = h = depth = ctype = interlace = None
    palette = trns = None
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if depth != 8 or interlace:
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        return None
    raw = zlib.decompress(idat)
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    bpp = channels
    i = 0
    for y in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if f == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    rgba = bytearray(w * h * 4)
    for k in range(w * h):
        if ctype == 6:
            rgba[4 * k:4 * k + 4] = out[4 * k:4 * k + 4]
        elif ctype == 2:
            rgba[4 * k:4 * k + 3] = out[3 * k:3 * k + 3]
            rgba[4 * k + 3] = 255
        elif ctype == 0:
            g = out[k]
            rgba[4 * k:4 * k + 4] = bytes((g, g, g, 255))
        elif ctype == 4:
            g = out[2 * k]
            rgba[4 * k:4 * k + 4] = bytes((g, g, g, out[2 * k + 1]))
        elif ctype == 3:
            idx = out[k]
            r, g, b = palette[3 * idx:3 * idx + 3] if palette and 3 * idx + 2 < len(palette) else (0, 0, 0)
            a = trns[idx] if trns and idx < len(trns) else 255
            rgba[4 * k:4 * k + 4] = bytes((r, g, b, a))
    return w, h, bytes(rgba)


SVG_SCALE = 2.0  # rasterize vectors at 2x so scaled-up sprites stay sharp


def rasterize(costume):
    """Sets costume.raster = (w, h, scale, rgba) or leaves it None."""
    if costume.asset is None or costume.w <= 0 or costume.h <= 0:
        return
    try:
        if costume.data_format == "svg":
            import resvg_py
            png = resvg_py.svg_to_bytes(svg_string=costume.asset.decode("utf-8", "replace"), zoom=SVG_SCALE)
            dec = decode_png(bytes(png))
            if dec:
                w, h, rgba = dec
                costume.raster = (w, h, w / costume.w, rgba)
        elif costume.data_format == "png":
            dec = decode_png(costume.asset)
            if dec:
                w, h, rgba = dec
                costume.raster = (w, h, w / costume.w, rgba)
    except Exception:
        costume.raster = None


def rasterize_all(prog):
    for s in prog.sprites:
        for c in s.costumes:
            rasterize(c)
