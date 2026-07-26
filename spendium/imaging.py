"""Receipt image handling: perceptual hashing and the deletion commitment.

The privacy policy promises receipt images are deleted within 24 hours of
processing, regardless of account status. That makes the image the most
short-lived thing in the system and the hash the only part that outlives it —
which is enough for duplicate detection, the cheapest high-value fraud check
available, and is why the hash is computed at upload rather than on demand.
"""

from io import BytesIO

from PIL import Image

# 8x8 comparisons need a 9-pixel-wide row: each bit compares a pixel with its
# right-hand neighbour.
_WIDTH = 9
_HEIGHT = 8


def perceptual_hash(image_bytes: bytes) -> str:
    """A difference hash of the image, as 16 hex characters.

    Difference hashing survives the things that vary innocently between two
    photographs of the same receipt — scale, compression, mild brightness shifts
    — while still changing when the content changes. A cryptographic hash would
    be useless here: re-photographing one receipt produces different bytes every
    time, so byte equality never fires on the abuse it is meant to catch.
    """
    with Image.open(BytesIO(image_bytes)) as image:
        grey = image.convert("L").resize((_WIDTH, _HEIGHT), Image.Resampling.LANCZOS)
        # One byte per pixel in row order for mode "L". Avoids getdata(), which
        # Pillow has deprecated.
        pixels = grey.tobytes()

    bits = 0
    for row in range(_HEIGHT):
        offset = row * _WIDTH
        for col in range(_HEIGHT):
            bits <<= 1
            if pixels[offset + col] > pixels[offset + col + 1]:
                bits |= 1
    return f"{bits:016x}"


def hamming_distance(left: str, right: str) -> int:
    """How many bits differ between two hashes. Lower means more similar.

    Raises on differing lengths rather than returning a large number, since a
    silently incomparable pair would read as "not a duplicate" and quietly
    disable the check.
    """
    if len(left) != len(right):
        raise ValueError("Hashes are not the same length.")
    return bin(int(left, 16) ^ int(right, 16)).count("1")
