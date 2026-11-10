"""A receipt photo, read and shrunk for Claude (2026-11-10):
utils/image_header (stdlib, no decoder) decides what may reach Pillow, and
utils/receipt_image — the ONE module that imports PIL — reduces it."""

import ast
import io
import os
import pathlib
import random
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image  # noqa: E402 — the tests BUILD their fixtures with it

from utils import image_header, receipt_image  # noqa: E402

_ATHENA = pathlib.Path(__file__).resolve().parent.parent


def _jpeg(width=640, height=480, *, mode="RGB", progressive=False,
          exif_orientation=None, quality=90) -> bytes:
    img = Image.new(mode, (width, height), (200, 30, 30) if mode == "RGB" else 128)
    kwargs = {"quality": quality, "progressive": progressive}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif.tobytes()
    out = io.BytesIO()
    img.save(out, "JPEG", **kwargs)
    return out.getvalue()


def _png(width=320, height=200, *, mode="RGB") -> bytes:
    colour = {"RGB": (0, 0, 255), "RGBA": (0, 0, 255, 0), "L": 90,
              "LA": (90, 0), "P": 3}[mode]
    out = io.BytesIO()
    Image.new(mode, (width, height), colour).save(out, "PNG")
    return out.getvalue()


# ── image_header ──────────────────────────────────────────────────────────


def test_a_png_is_read_from_its_ihdr():
    for mode, channels in (("RGB", 3), ("RGBA", 4), ("L", 1), ("LA", 2), ("P", 1)):
        info = image_header.image_info(_png(321, 123, mode=mode), "image/png")
        assert info == image_header.ImageInfo("PNG", 321, 123, channels), mode


def test_a_baseline_or_progressive_jpeg_is_read_from_its_frame():
    for progressive in (False, True):
        info = image_header.image_info(_jpeg(641, 479, progressive=progressive),
                                       "image/jpeg")
        assert info == image_header.ImageInfo("JPEG", 641, 479, 3)
    assert image_header.image_info(_jpeg(64, 48, mode="L"), "image/jpeg") == (
        image_header.ImageInfo("JPEG", 64, 48, 1))


def test_a_cmyk_jpeg_is_never_accepted():
    assert image_header.image_info(_jpeg(64, 48, mode="CMYK"), "image/jpeg") is None


@pytest.mark.parametrize("marker", [0xC3, 0xC5, 0xC9, 0xCB, 0xCF])
def test_a_lossless_hierarchical_or_arithmetic_jpeg_is_refused(marker):
    # SOF3 is the lossless frame whose scaled decode a 2026 Pillow advisory
    # broke: answered « unsupported » here, before any decoder.
    frame = bytes([0xFF, marker, 0x00, 0x0B, 8, 0x01, 0x00, 0x01, 0x00, 3, 1, 0x11, 0])
    assert image_header.image_info(b"\xff\xd8" + frame, "image/jpeg") is None


@pytest.mark.parametrize("data, mime", [
    (b"", "image/jpeg"),
    (b"\xff\xd8", "image/jpeg"),
    (b"\xff\xd8\xff\xe0\xff\xff", "image/jpeg"),        # a length past the end
    (b"\xff\xd8\xff\xda\x00\x08" + b"\x00" * 8, "image/jpeg"),  # scan before frame
    (b"\xff\xd8\x00\x00", "image/jpeg"),                # not at a marker
    (b"\x89PNG\r\n\x1a\n", "image/png"),                # truncated
    (_png()[:20], "image/png"),
    (None, "image/png"),
])
def test_a_malformed_header_answers_none_never_an_exception(data, mime):
    assert image_header.image_info(data, mime) is None


def test_the_header_must_be_the_type_the_entry_stored():
    assert image_header.image_info(_png(), "image/jpeg") is None
    assert image_header.image_info(_jpeg(), "image/png") is None
    assert image_header.image_info(_jpeg(), "image/tiff") is None


def test_a_zero_dimension_is_refused():
    png = bytearray(_png(10, 10))
    png[16:20] = (0).to_bytes(4, "big")
    assert image_header.image_info(bytes(png), "image/png") is None


def test_the_marker_walk_is_linear():
    """A flood of standalone markers and fill bytes is walked once."""
    flood = b"\xff\xd8" + b"\xff\xd0" * 400_000 + b"\xff" * 400_000 + b"\xd9"
    start = time.perf_counter()
    assert image_header.image_info(flood, "image/jpeg") is None
    assert time.perf_counter() - start < 2.0


# ── receipt_image ─────────────────────────────────────────────────────────


def test_a_phone_photo_is_reduced_to_claude_s_reading_size():
    data = _jpeg(4032, 3024)
    out = receipt_image.prepare_for_claude(data, "image/jpeg")
    assert out.mime_type == "image/jpeg"
    assert (out.width, out.height) == (1568, 1176)
    assert (out.source_width, out.source_height) == (4032, 3024)
    assert len(out.data) <= receipt_image.OUTPUT_MAX_BYTES
    assert Image.open(io.BytesIO(out.data)).format == "JPEG"


def test_a_small_photo_is_never_enlarged():
    out = receipt_image.prepare_for_claude(_jpeg(800, 600), "image/jpeg")
    assert (out.width, out.height) == (800, 600)


def test_the_exif_orientation_is_applied():
    # Orientation 6: stored landscape, shown portrait — the receipt reads
    # upright for Claude.
    out = receipt_image.prepare_for_claude(
        _jpeg(1600, 1200, exif_orientation=6), "image/jpeg")
    assert out.height > out.width


def test_a_transparent_png_is_flattened_onto_white():
    out = receipt_image.prepare_for_claude(_png(100, 80, mode="RGBA"), "image/png")
    pixel = Image.open(io.BytesIO(out.data)).convert("RGB").getpixel((50, 40))
    assert min(pixel) > 240                 # white, not black


def test_a_refused_header_never_reaches_a_decoder(monkeypatch):
    def _never(*_a, **_k):
        pytest.fail("Pillow opened a photo its header had refused")

    monkeypatch.setattr(Image, "open", _never)
    for data, mime, reason in (
        (_jpeg(64, 48, mode="CMYK"), "image/jpeg", "image_unsupported"),
        (_png(), "image/jpeg", "image_unsupported"),
        (b"not an image", "image/png", "image_unsupported"),
    ):
        with pytest.raises(receipt_image.ImageRefused) as exc:
            receipt_image.prepare_for_claude(data, mime)
        assert exc.value.reason == reason


def test_an_oversized_photo_is_refused_from_its_header(monkeypatch):
    monkeypatch.setattr(Image, "open",
                        lambda *a, **k: pytest.fail("decoded an oversized photo"))
    wide = bytearray(_png(10, 10))
    wide[16:20] = (receipt_image.MAX_SIDE + 1).to_bytes(4, "big")
    huge = bytearray(_png(10, 10))
    huge[16:20] = (5000).to_bytes(4, "big")
    huge[20:24] = (5000).to_bytes(4, "big")      # 25 MP of PNG: whole decode
    for data in (bytes(wide), bytes(huge)):
        with pytest.raises(receipt_image.ImageRefused) as exc:
            receipt_image.prepare_for_claude(data, "image/png")
        assert exc.value.reason == "image_too_large"


def test_a_corrupt_body_behind_a_good_header_is_invalid():
    data = _jpeg(640, 480)
    with pytest.raises(receipt_image.ImageRefused) as exc:
        receipt_image.prepare_for_claude(data[:400], "image/jpeg")
    assert exc.value.reason == "image_invalid"


def test_a_photo_that_stays_too_heavy_is_refused(monkeypatch):
    rng = random.Random(7)
    noise = Image.frombytes("RGB", (900, 900), bytes(rng.getrandbits(8)
                                                     for _ in range(900 * 900 * 3)))
    out = io.BytesIO()
    noise.save(out, "PNG")
    monkeypatch.setattr(receipt_image, "OUTPUT_MAX_BYTES", 20_000)
    with pytest.raises(receipt_image.ImageRefused) as exc:
        receipt_image.prepare_for_claude(out.getvalue(), "image/png")
    assert exc.value.reason == "image_too_large"


def test_only_receipt_image_imports_pil():
    """requirements.in's Pillow note: no Athena module imports PIL but
    utils/receipt_image, which reaches it only inside its one call."""
    importers = []
    for path in sorted(_ATHENA.rglob("*.py")):
        rel = path.relative_to(_ATHENA).as_posix()
        if rel.startswith(("tests/", ".venv/", "venv/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom)
                     else [])
            if any(n == "PIL" or n.startswith("PIL.") for n in names):
                importers.append(rel)
    assert sorted(set(importers)) == ["utils/receipt_image.py"]
    # ...and not at import time: inside prepare_for_claude.
    tree = ast.parse((_ATHENA / "utils" / "receipt_image.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(getattr(n, "module", "") == "PIL" for n in top)
