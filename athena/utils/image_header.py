"""An image's dimensions read from its header bytes alone — stdlib, no
decoder (2026-11-10).

The receipt read (``get_admin_receipt``) decides whether a photo may reach
Pillow BEFORE Pillow sees a byte of it (``utils/receipt_image``): a JPEG must
be baseline or progressive Huffman — SOF0, SOF1 or SOF2 — with 1 or 3
components; a lossless (SOF3), hierarchical or arithmetic-coded JPEG, or a
4-component (CMYK/YCCK) one, is answered « unsupported » here, and never
decoded. A PNG is read from its IHDR chunk, which the format puts first.

The walk is linear and bounded by the data itself: each step moves forward,
nothing is allocated in proportion to a size the file declares, and a
truncated or malformed header answers ``None`` — never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

JPEG = "JPEG"
PNG = "PNG"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The Huffman SOF markers a receipt photo is written with: baseline,
# extended sequential and progressive.
_SOF_SUPPORTED = {0xC0, 0xC1, 0xC2}
# Every other start-of-frame: lossless, hierarchical, arithmetic-coded.
_SOF_OTHER = {0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# Markers that stand alone, with no length field.
_STANDALONE = {0x01} | set(range(0xD0, 0xD8))
# PNG colour type → channels.
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


@dataclass(frozen=True)
class ImageInfo:
    format: str        # "JPEG" | "PNG"
    width: int
    height: int
    components: int    # JPEG: 1 or 3; PNG: the colour type's channels


def image_info(data: bytes, mime_type: str) -> Optional[ImageInfo]:
    """The format, size and channels of *data*, read from its header — or
    ``None`` when it is not a JPEG or PNG this module accepts (a format
    other than *mime_type* says, an unsupported JPEG, a malformed or
    truncated header, a zero dimension)."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    if mime_type == "image/jpeg":
        return _jpeg(bytes(data))
    if mime_type == "image/png":
        return _png(bytes(data))
    return None


def _jpeg(data: bytes) -> Optional[ImageInfo]:
    if not data.startswith(b"\xff\xd8"):
        return None
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            return None                       # not at a marker: malformed
        while i < n and data[i] == 0xFF:      # fill bytes before a marker
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in _STANDALONE:
            continue
        if marker in (0xD9, 0xDA):            # EOI or scan data before a frame
            return None
        if i + 2 > n:
            return None
        length = (data[i] << 8) | data[i + 1]
        if length < 2 or i + length > n:
            return None
        if marker in _SOF_OTHER:
            return None
        if marker in _SOF_SUPPORTED:
            if length < 8:
                return None
            height = (data[i + 3] << 8) | data[i + 4]
            width = (data[i + 5] << 8) | data[i + 6]
            components = data[i + 7]
            if not width or not height or components not in (1, 3):
                return None
            return ImageInfo(JPEG, width, height, components)
        i += length
    return None


def _png(data: bytes) -> Optional[ImageInfo]:
    # signature (8) + IHDR length (4) + "IHDR" (4) + width, height (8) +
    # bit depth (1) + colour type (1)
    if len(data) < 26 or not data.startswith(_PNG_SIGNATURE):
        return None
    if data[12:16] != b"IHDR" or int.from_bytes(data[8:12], "big") != 13:
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    channels = _PNG_CHANNELS.get(data[25])
    if not width or not height or channels is None:
        return None
    return ImageInfo(PNG, width, height, channels)
