"""Generate the app icon set from one geometric mark.

The icons are load-bearing — without a valid one nothing offers to install the
site — and there is no design tooling in this project. So they are drawn in code
rather than exported from somewhere: a mark that can be regenerated is one that
can be adjusted later without hunting for an original file nobody kept.

An HF monogram, with the letters *drawn* rather than set. Typesetting them would
mean shipping and depending on a font file, and would leave the weight at
whatever the face happened to give; constructing them from bars keeps the stroke
thick enough to survive the 48px Android actually renders on a home screen, and
keeps the result identical on every machine that runs this.

The two letters share a stem — the H's right upright is also the F's — which is
what makes it read as one mark instead of two initials, and keeps the stroke
count low enough to stay legible when it is small.

The PNGs are committed. This command exists to change them, not to build them on
deploy.
"""

from pathlib import Path

from django.core.management.base import BaseCommand
from PIL import Image, ImageDraw

# The brand palette, from static/css/nav.css.
GREEN = (45, 90, 39)  # --nav-green
CREAM = (245, 243, 238)

# Everything is drawn large and reduced once. The bars are axis-aligned, so this
# matters less than it did for the rotated shapes it replaced, but it still keeps
# the corner radii clean at 192px and costs nothing.
SUPERSAMPLE = 4

# Proportions, all as multiples of cap height.
STROKE = 0.19  # bold: thin strokes disappear at home-screen size
SPAN = 0.52  # left upright to the shared stem
ARM = 0.40  # how far the F reaches right
ASPECT = SPAN + STROKE + ARM  # mark width ÷ cap height

# What fraction of the tile the mark occupies.
#
# `any` icons are shown as drawn, so the mark can be generous. A `maskable` icon
# is cropped by the platform to whatever shape it likes — Android uses a circle
# of 80% diameter — and anything outside that is liable to be cut, so the mark
# has to be much smaller. Getting this wrong is the difference between an icon
# and a green circle with a fragment of leaf in it.
MARK_ANY = 0.66
MARK_MASKABLE = 0.52


def _monogram(draw: ImageDraw.ImageDraw, left: float, top: float, cap: float) -> None:
    """HF as four bars.

    A true ligature: the letters share both the upright *and* the crossbar. Give
    the F its own middle arm and it lands a hair below the H's bar, leaving a
    cramped slot between two near-parallel strokes that closes up entirely at
    small sizes. Merging them is cleaner and loses nothing — read left of the
    shared upright and it is an H, read right of it and it is an F.
    """
    stroke = cap * STROKE
    radius = stroke * 0.18
    stem = left + cap * SPAN
    reach = stem + stroke + cap * ARM

    def bar(x0: float, y0: float, x1: float, y1: float) -> None:
        draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=CREAM)

    bar(left, top, left + stroke, top + cap)  # H, left upright
    bar(stem, top, stem + stroke, top + cap)  # shared upright
    bar(stem, top, reach, top + stroke)  # F, top arm

    # The shared bar, running the full width. Stopping short of the top arm is
    # what makes the right-hand letter an F rather than an E missing its foot.
    crossbar = top + (cap - stroke) / 2
    bar(left, crossbar, reach - cap * ARM * 0.30, crossbar + stroke)


def render(size: int, mark_fraction: float) -> Image.Image:
    """The monogram on a solid tile.

    Full-bleed and fully opaque: iOS applies its own corner rounding to
    apple-touch-icon and renders transparency as black, so both would be defects
    here rather than features.
    """
    canvas = size * SUPERSAMPLE
    image = Image.new("RGB", (canvas, canvas), GREEN)

    # `mark_fraction` sizes the wider axis, so the fraction means the same thing
    # here as it did for a square mark and the safe-area maths still holds.
    width = canvas * mark_fraction
    cap = width / ASPECT
    _monogram(
        ImageDraw.Draw(image),
        left=(canvas - width) / 2,
        top=(canvas - cap) / 2,
        cap=cap,
    )

    return image.resize((size, size), Image.LANCZOS)


class Command(BaseCommand):
    help = "Regenerate the PWA and iOS icons in static/icons/."

    def handle(self, *args, **options) -> None:
        # Written to the source tree, not STATIC_ROOT: these are inputs to
        # collectstatic, not outputs of it.
        target = Path(__file__).resolve().parents[3] / "static" / "icons"
        target.mkdir(parents=True, exist_ok=True)

        icons = {
            "icon-192.png": (192, MARK_ANY),
            "icon-512.png": (512, MARK_ANY),
            "icon-maskable-512.png": (512, MARK_MASKABLE),
            # iOS picks this up for Add to Home Screen and ignores the manifest
            # icons entirely. 180px is what current iPhones ask for.
            "apple-touch-icon.png": (180, MARK_ANY),
        }

        for name, (size, fraction) in icons.items():
            render(size, fraction).save(target / name, "PNG", optimize=True)
            self.stdout.write(f"Wrote {name} ({size}x{size})")
