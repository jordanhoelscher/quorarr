#!/usr/bin/env python3
"""Generate the PWA icons.

Draws the "Q" monogram — a luminous ring with a struck tail, on basin stone —
at 192px and 512px, maskable-safe (all ink sits inside the inner 80% circle).
Geometry matches ``public/favicon.svg``; keep the two in step if either
changes.

The letter is the *project's* mark (Quorarr), not the instance's: an operator
who renames their deployment with APP_NAME keeps these unless they replace
the files, which is the one piece of branding config cannot reach.

Uses Pillow rather than ImageMagick because Skynet has no ``convert`` binary.

Usage:
    python3 scripts/make-icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# Palette, mirrored from src/app.css.
STONE_TOP = (13, 22, 34)
STONE_BOTTOM = (4, 6, 10)
MEMORY = (124, 224, 230)
WHITE = (255, 255, 255)

# Monogram geometry, as fractions of the canvas edge. The ring's diameter is
# measured to the stroke centreline, so the painted outer edge sits half a
# stroke beyond RING_* — which is what keeps the ink inside the maskable
# safe circle.
STROKE = 0.088
RING_LEFT = 0.30
RING_RIGHT = 0.70
RING_TOP = 0.26
RING_BOTTOM = 0.74
#: The tail: a short bar struck through the lower-right of the ring at 45
#: degrees, given as centreline endpoints.
TAIL_FROM = (0.575, 0.605)
TAIL_TO = (0.755, 0.785)

SUPERSAMPLE = 4
OUT_DIR = Path(__file__).resolve().parent.parent / "public" / "icons"
SIZES = (192, 512)


def stone(size: int) -> Image.Image:
    """Basin backdrop: a vertical stone gradient lit by a cold radial glow."""
    # Built small and upscaled — a 64px gradient resamples to a perfectly
    # smooth field far faster than a per-pixel loop at 2048px.
    small = 64
    base = Image.new("RGB", (1, small))
    for y in range(small):
        t = y / (small - 1)
        base.putpixel(
            (0, y),
            tuple(round(a + (b - a) * t) for a, b in zip(STONE_TOP, STONE_BOTTOM)),
        )
    canvas = base.resize((size, size), Image.Resampling.BICUBIC)

    # Cold vapour glow, centred slightly above the middle.
    glow_px = 96
    glow_mask = Image.new("L", (glow_px, glow_px), 0)
    gd = ImageDraw.Draw(glow_mask)
    cx, cy = glow_px / 2, glow_px * 0.42
    for step in range(28, 0, -1):
        radius = glow_px * 0.55 * step / 28
        gd.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=int(58 * (1 - step / 28) ** 1.6),
        )
    glow_mask = glow_mask.resize((size, size), Image.Resampling.BICUBIC)
    canvas.paste(Image.new("RGB", (size, size), MEMORY), (0, 0), glow_mask)
    return canvas


def monogram_mask(size: int) -> Image.Image:
    """Alpha mask of the "Q": a stroked ring with a struck tail."""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)

    stroke = STROKE * size

    # Ring. `ellipse` with a width strokes *inward* from the given box, so the
    # box is inflated by half a stroke to put the centreline on RING_*.
    half = stroke / 2
    d.ellipse(
        (
            RING_LEFT * size - half,
            RING_TOP * size - half,
            RING_RIGHT * size + half,
            RING_BOTTOM * size + half,
        ),
        outline=255,
        width=round(stroke),
    )

    # Tail — a round-capped bar. `line` with `joint`/caps is unreliable across
    # Pillow versions, so the caps are drawn as explicit discs; that also
    # matches the SVG's `stroke-linecap="round"`.
    x0, y0 = TAIL_FROM[0] * size, TAIL_FROM[1] * size
    x1, y1 = TAIL_TO[0] * size, TAIL_TO[1] * size
    d.line((x0, y0, x1, y1), fill=255, width=round(stroke))
    for cx, cy in ((x0, y0), (x1, y1)):
        d.ellipse((cx - half, cy - half, cx + half, cy + half), fill=255)
    return mask


def strand(size: int) -> Image.Image:
    """White-to-cyan vertical wash used to ink the monogram."""
    small = 32
    base = Image.new("RGB", (1, small))
    for y in range(small):
        t = y / (small - 1)
        base.putpixel(
            (0, y),
            tuple(round(a + (b - a) * t) for a, b in zip(WHITE, MEMORY)),
        )
    return base.resize((size, size), Image.Resampling.BICUBIC)


def build(size: int) -> Image.Image:
    work = size * SUPERSAMPLE
    canvas = stone(work)
    mask = monogram_mask(work)

    # Halo first, so the letterform reads as lit rather than painted on.
    halo = mask.filter(ImageFilter.GaussianBlur(work * 0.035)).point(
        lambda v: int(v * 0.55)
    )
    canvas.paste(Image.new("RGB", (work, work), MEMORY), (0, 0), halo)
    canvas.paste(strand(work), (0, 0), mask)

    return canvas.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT_DIR / f"icon-{size}.png"
        build(size).save(path, "PNG", optimize=True)
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
