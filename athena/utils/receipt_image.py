"""A receipt photo shrunk for Claude — the ONE module that calls Pillow
(2026-11-10).

The lawyer decided (2026-11-10) that Claude may SEE a photographed
pièce justificative through the connector: ``get_admin_receipt`` hands the
picture itself back, as an MCP image block. claude.ai reportedly refuses a
tool result over about 1 MB (third-party reports, not Anthropic's documents),
and Claude reads an image at about 1 568 px on its long side anyway, so a
phone photo (1 to 5 MB) is reduced here before it leaves.

Two house rules make an exception for this module, declared here as the
house requires:

* « Stored files never transit Flask » (CLAUDE.md, Security Rules): a
  receipt photo is read into memory (≤ ``MAX_RECEIPT_SIZE``, 10 MB) and a
  REDUCED copy of it, ≤ :data:`OUTPUT_MAX_BYTES`, goes out in the tool's
  answer — never a link, never the original bytes;
* « No Athena module imports PIL » (requirements.in, the Pillow note): this
  one does, and the note says so. What reaches Pillow is bounded:

  1. only a type ``models/document._sniff_header`` recognised and the entry
     stored as ``image/jpeg`` or ``image/png`` (the caller checks);
  2. the header is read FIRST, by ``utils/image_header`` — a baseline or
     progressive JPEG with 1 or 3 components, or a PNG — and its size is
     checked against :data:`MAX_SIDE` and the pixel caps, before any
     decoder runs;
  3. ``Image.open(..., formats=("JPEG", "PNG"))``: no other plugin is ever
     tried;
  4. a JPEG decodes through ``draft()`` (DCT scaling: a 12 MP photo decodes
     at a quarter of its pixels or less), the EXIF orientation is applied,
     the picture is reduced to :data:`LONG_SIDE` px and re-encoded as JPEG
     (quality 80, then 60), and refused above :data:`OUTPUT_MAX_BYTES`.

Pure: no Flask, no models. A refusal is :class:`ImageRefused` with a
machine-stable ``reason`` the handler words in French.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from utils import image_header

#: Claude's own reading size (the long side it scales an image to).
LONG_SIDE = 1568
#: No side of an accepted photo is longer (a 50 MP camera is ~8 200 px).
MAX_SIDE = 12_000
#: Pixels: a JPEG decodes reduced (draft), a PNG whole — hence two caps. A
#: JPEG ``draft()`` cannot reduce is held to the whole-decode cap too.
JPEG_MAX_PIXELS = 50_000_000
FULL_DECODE_MAX_PIXELS = 16_000_000
#: The reduced JPEG's ceiling: base64 grows it by a third, and the answer
#: must stay under claude.ai's reported ~1 MB with its text beside it.
OUTPUT_MAX_BYTES = 700 * 1024
QUALITIES = (80, 60)

REASON_UNSUPPORTED = "image_unsupported"
REASON_TOO_LARGE = "image_too_large"
REASON_INVALID = "image_invalid"


class ImageRefused(Exception):
    """The photo cannot be handed to Claude; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PreparedImage:
    data: bytes            # a JPEG
    mime_type: str         # "image/jpeg"
    width: int
    height: int
    source_width: int
    source_height: int


def _fit(width: int, height: int, long_side: int) -> tuple[int, int]:
    """*width* × *height* scaled down to fit *long_side* (never up)."""
    scale = min(1.0, long_side / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def prepare_for_claude(data: bytes, mime_type: str) -> PreparedImage:
    """A reduced JPEG of the receipt photo *data* (stored as *mime_type*),
    or :class:`ImageRefused`. See the module docstring for every bound."""
    info = image_header.image_info(data, mime_type)
    if info is None:
        raise ImageRefused(REASON_UNSUPPORTED)
    pixels = info.width * info.height
    cap = (JPEG_MAX_PIXELS if info.format == image_header.JPEG
           else FULL_DECODE_MAX_PIXELS)
    if info.width > MAX_SIDE or info.height > MAX_SIDE or pixels > cap:
        raise ImageRefused(REASON_TOO_LARGE)

    # Imported here, so that only this call — never an import of the module
    # — puts Pillow's decoders to work.
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data), formats=(info.format,)) as img:
            if img.size != (info.width, info.height):
                raise ImageRefused(REASON_INVALID)   # the header lied
            if info.format == image_header.JPEG:
                drafted = img.draft("RGB", _fit(info.width, info.height,
                                                LONG_SIDE))
                if drafted is None and pixels > FULL_DECODE_MAX_PIXELS:
                    raise ImageRefused(REASON_TOO_LARGE)
            picture = ImageOps.exif_transpose(img)
            picture.thumbnail((LONG_SIDE, LONG_SIDE))
            picture = _as_rgb(picture, Image)
            for quality in QUALITIES:
                out = io.BytesIO()
                picture.save(out, "JPEG", quality=quality, optimize=True)
                if out.tell() <= OUTPUT_MAX_BYTES:
                    return PreparedImage(
                        out.getvalue(), "image/jpeg", picture.width,
                        picture.height, info.width, info.height,
                    )
    except ImageRefused:
        raise
    except Exception as exc:   # every decoder error, the bomb guard's too
        raise ImageRefused(REASON_INVALID) from exc
    raise ImageRefused(REASON_TOO_LARGE)


def _as_rgb(picture, Image):
    """RGB for the JPEG encoder: transparency flattened onto white."""
    if picture.mode == "RGB":
        return picture
    if picture.mode in ("RGBA", "LA") or (
            picture.mode == "P" and "transparency" in picture.info):
        rgba = picture.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return picture.convert("RGB")
