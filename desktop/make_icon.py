"""Generate build/icon.icns (a tennis ball) with Pillow + sips + iconutil.

Only used by desktop/build_dmg.sh; keeps the repo free of binary assets.
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"


def draw(png: Path) -> None:
    from PIL import Image, ImageDraw

    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = 40
    d.ellipse((pad, pad, size - pad, size - pad), fill=(205, 235, 60, 255))

    # two seams: white arcs sweeping across the ball
    width = 34
    for flip in (False, True):
        points = []
        for i in range(0, 181, 2):
            t = math.radians(180 + i)          # left half arc
            x = size / 2 + math.cos(t) * (size / 2 - pad - 12)
            y = size / 2 + math.sin(t) * (size / 2 - pad - 12) * 0.62
            if flip:
                x = size - x
            points.append((x, y))
        d.line(points, fill=(255, 255, 255, 255), width=width, joint="curve")

    img.save(png)


def main() -> int:
    BUILD.mkdir(exist_ok=True)
    base = BUILD / "icon-1024.png"
    try:
        draw(base)
    except ImportError:
        print("pillow not installed — skipping icon", file=sys.stderr)
        return 0

    iconset = BUILD / "icon.iconset"
    if iconset.exists():
        for old in iconset.iterdir():
            old.unlink()
    iconset.mkdir(exist_ok=True)
    for px in (16, 32, 64, 128, 256, 512):
        for scale in (1, 2):
            target = iconset / f"icon_{px}x{px}{'@2x' if scale == 2 else ''}.png"
            subprocess.run(["sips", "-z", str(px * scale), str(px * scale),
                            str(base), "--out", str(target)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["iconutil", "-c", "icns", str(iconset),
                    "-o", str(BUILD / "icon.icns")], check=True)
    print(f"icon: {BUILD / 'icon.icns'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
