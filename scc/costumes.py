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
