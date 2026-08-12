"""Monoline icons for the pub2pdf UI, drawn with PIL — no external asset files,
so the one-file PyInstaller build stays one file. Each is drawn on a 4x canvas
and downscaled with LANCZOS for clean anti-aliased strokes, then wrapped in a
CTkImage. Lucide-style: single weight, round caps, generous padding.
"""

from functools import lru_cache

import customtkinter as ctk
from PIL import Image, ImageDraw

_SCALE = 4


def _canvas(px: int):
    s = px * _SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img), s


def _stroke(s: int) -> int:
    return max(_SCALE, round(s * 0.075))


def _round_line(d, pts, color, w):
    d.line(pts, fill=color, width=w, joint="curve")
    r = w / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _draw(name: str, s: int, color: str, d: ImageDraw.ImageDraw) -> None:
    w = _stroke(s)
    if name == "download":                      # arrow into a tray — drop zone
        cx = s * 0.5
        _round_line(d, [(cx, s * 0.16), (cx, s * 0.60)], color, w)
        _round_line(d, [(s * 0.32, s * 0.44), (cx, s * 0.62), (s * 0.68, s * 0.44)], color, w)
        _round_line(d, [(s * 0.22, s * 0.82), (s * 0.78, s * 0.82)], color, w)
    elif name == "search":                      # magnifier — scan
        r = s * 0.26
        cx, cy = s * 0.42, s * 0.42
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=w)
        _round_line(d, [(cx + r * 0.72, cy + r * 0.72), (s * 0.82, s * 0.82)], color, w)
    elif name == "folder":                      # folder — save-to
        d.rounded_rectangle([s * 0.16, s * 0.34, s * 0.84, s * 0.78],
                            radius=s * 0.06, outline=color, width=w)
        _round_line(d, [(s * 0.16, s * 0.34), (s * 0.30, 0.34 * s),
                        (s * 0.38, s * 0.24), (s * 0.52, s * 0.24)], color, w)
    elif name == "x":                           # remove
        _round_line(d, [(s * 0.30, s * 0.30), (s * 0.70, s * 0.70)], color, w)
        _round_line(d, [(s * 0.70, s * 0.30), (s * 0.30, s * 0.70)], color, w)
    elif name == "check":                       # done
        _round_line(d, [(s * 0.24, s * 0.52), (s * 0.43, s * 0.70), (s * 0.76, s * 0.30)], color, w)
    elif name == "alert":                       # failed
        _round_line(d, [(s * 0.5, s * 0.20), (s * 0.16, s * 0.80),
                        (s * 0.84, s * 0.80), (s * 0.5, s * 0.20)], color, w)
        _round_line(d, [(s * 0.5, s * 0.44), (s * 0.5, s * 0.62)], color, w)
        d.ellipse([s * 0.5 - w * 0.6, s * 0.70 - w * 0.6,
                   s * 0.5 + w * 0.6, s * 0.70 + w * 0.6], fill=color)
    elif name == "clock":                       # not converted / pending
        r = s * 0.32
        cx = cy = s * 0.5
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=w)
        _round_line(d, [(cx, cy - r * 0.52), (cx, cy)], color, w)
        _round_line(d, [(cx, cy), (cx + r * 0.5, cy)], color, w)
    elif name == "spinner":                     # scanning (a 3/4 arc)
        r = s * 0.30
        cx = cy = s * 0.5
        d.arc([cx - r, cy - r, cx + r, cy + r], start=0, end=270, fill=color, width=w)


def _render(name: str, size: int, color: str) -> Image.Image:
    img, d, s = _canvas(size)
    _draw(name, s, color, d)
    return img.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=256)
def icon(name: str, size: int = 18, light: str = "#39424e", dark: str = "#c2ccd8") -> ctk.CTkImage:
    """Theme-aware monoline icon: separate light/dark renders so it follows the
    Windows appearance mode like the rest of the palette."""
    return ctk.CTkImage(light_image=_render(name, size, light),
                        dark_image=_render(name, size, dark), size=(size, size))
