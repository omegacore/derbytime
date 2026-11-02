"""Move rank emblem masters into assets/rank-emblems/ and build web PNGs in
static/ranks/: white surround made transparent, trimmed, 512px on the long side."""
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None
ROOT = Path(sys.argv[1])
SRC = {
    "lion": "Lion-insignia-CSBC.jpg",
    "tiger": "TIGER-color-insignia-CSBC.jpg",
    "wolf": "Wolf rank-color-insignia-CSBC.jpg",
    "bear": "CS_bear-insignia-CSBC.jpg",
    "webelos": "Webelos Oval-color-insignia-CSBC.jpg",
    "aol": "Arrow of Light-insignia-CSBC.jpg",
}
MASTERS = ROOT / "assets" / "rank-emblems"
WEB = ROOT / "src" / "derbytime" / "static" / "ranks"
SENTINEL = (1, 254, 3)
OUT = 512

MASTERS.mkdir(parents=True, exist_ok=True)
for rank, name in SRC.items():
    src = ROOT / name  # original download name, if a new copy was dropped in the project root
    master = MASTERS / f"{rank}{src.suffix.lower()}"
    if src.exists():
        shutil.move(str(src), master)
    if not master.exists():
        print(f"{rank}: no master, skipped")
        continue
    im = Image.open(master).convert("RGB")
    im.thumbnail((1400, 1400), Image.LANCZOS)  # work size: big enough for clean edges

    # Flood the white surround from the edges, then turn it transparent.
    filled = im.copy()
    w, h = im.size
    seeds = [(x, y) for x in range(0, w, max(1, w // 40)) for y in (0, h - 1)]
    seeds += [(x, y) for y in range(0, h, max(1, h // 40)) for x in (0, w - 1)]
    for xy in seeds:
        r, g, b = filled.getpixel(xy)
        if min(r, g, b) > 225 and (r, g, b) != SENTINEL:
            ImageDraw.floodfill(filled, xy, SENTINEL, thresh=60)
    alpha = Image.eval(
        Image.merge("RGB", [c.point(lambda v, t=t: 255 if v == t else 0) for c, t in zip(filled.split(), SENTINEL)]).convert("L"),
        lambda v: 0 if v == 255 else 255,
    )
    rgba = im.copy()
    rgba.putalpha(alpha)
    rgba = rgba.crop(alpha.getbbox())

    # Longest side OUT px, downscaled premultiplied so edges don't pick up white.
    k = OUT / max(rgba.size)
    sq = rgba.convert("RGBa").resize((round(rgba.width * k), round(rgba.height * k)), Image.LANCZOS).convert("RGBA")
    out = WEB / f"{rank}.png"
    sq.save(out, optimize=True)
    print(f"{rank}: {master.name} -> {out.name} ({out.stat().st_size // 1024} KB)")
