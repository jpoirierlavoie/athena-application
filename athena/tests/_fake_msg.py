"""A minimal compound-file (OLE2/CFB, version 3) WRITER for the .msg tests —
synthetic fixtures only: this repository is public, so no real Outlook
export (a sender's address, headers, a mailbox's ids) ever enters it.

``build_cfb({"path/inside": bytes, ...})`` writes a valid compound file:
512-byte sectors and a FAT; streams under 4 096 bytes in the mini stream
(64-byte sectors, a mini FAT), larger ones in regular sectors; storages
created from the paths' folders; each storage's children chained through
their right siblings (a degenerate but valid tree). olefile — what the code
under test reads with — reads it back; tests/test_msg_text.py proves it.

``msg_streams(...)`` builds the named MAPI streams of a message
([MS-OXMSG]): ``__substg1.0_PPPPTTTT`` string properties (UTF-16LE), the
root's ``__properties_version1.0`` with a submit time, and attachment
storages.
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone

SECTOR, MINI, CUTOFF = 512, 64, 4096
ENDOFCHAIN, FREESECT, FATSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
_ENTRIES_PER_SECTOR = SECTOR // 128


def _chunks(data: bytes, size: int) -> list[bytes]:
    out = [data[i:i + size] for i in range(0, len(data), size)] or []
    if out and len(out[-1]) < size:
        out[-1] = out[-1] + b"\x00" * (size - len(out[-1]))
    return out


def build_cfb(streams: dict[str, bytes]) -> bytes:
    # 1. The directory: the root, the storages the paths name, the streams.
    entries = [{"name": "Root Entry", "type": 5, "children": [], "data": None}]
    index = {"": 0}
    for path, data in streams.items():
        parts = path.split("/")
        for depth in range(len(parts)):
            key = "/".join(parts[:depth + 1])
            if key in index:
                continue
            is_stream = depth == len(parts) - 1
            entries.append({"name": parts[depth], "type": 2 if is_stream else 1,
                            "children": [], "data": data if is_stream else None})
            index[key] = len(entries) - 1
            entries[index["/".join(parts[:depth])]]["children"].append(index[key])

    # 2. The bytes: small streams in the mini stream, the others in sectors.
    sectors: list[bytes] = []
    fat: list[int] = []

    def allocate(data: bytes) -> int:
        chunks = _chunks(data, SECTOR)
        if not chunks:
            return ENDOFCHAIN
        start = len(sectors)
        for k, chunk in enumerate(chunks):
            sectors.append(chunk)
            fat.append(start + k + 1 if k < len(chunks) - 1 else ENDOFCHAIN)
        return start

    ministream, minifat = bytearray(), []
    for entry in entries:
        if entry["type"] != 2:
            continue
        data = entry["data"]
        entry["size"] = len(data)
        if not data:
            entry["start"] = ENDOFCHAIN
        elif len(data) < CUTOFF:
            chunks = _chunks(data, MINI)
            start = len(minifat)
            for k, chunk in enumerate(chunks):
                ministream += chunk
                minifat.append(start + k + 1 if k < len(chunks) - 1 else ENDOFCHAIN)
            entry["start"] = start
        else:
            entry["start"] = allocate(data)
    entries[0]["start"] = allocate(bytes(ministream)) if ministream else ENDOFCHAIN
    entries[0]["size"] = len(ministream)
    minifat_start = (allocate(b"".join(struct.pack("<I", v) for v in minifat))
                     if minifat else ENDOFCHAIN)
    minifat_sectors = -(-len(minifat) * 4 // SECTOR) if minifat else 0

    # 3. The directory sectors: 128-byte entries, siblings chained right.
    for entry in entries:
        kids = entry["children"]
        entry["child"] = kids[0] if kids else NOSTREAM
        for a, b in zip(kids, kids[1:]):
            entries[a]["right"] = b
    raw = bytearray()
    for entry in entries:
        name = entry["name"].encode("utf-16-le") + b"\x00\x00"
        raw += name.ljust(64, b"\x00")
        raw += struct.pack("<HBB", len(name), entry["type"], 1)
        raw += struct.pack("<III", NOSTREAM, entry.get("right", NOSTREAM),
                           entry["child"])
        raw += b"\x00" * 16 + b"\x00" * 4 + b"\x00" * 16      # clsid, state, times
        raw += struct.pack("<IQ", entry.get("start", 0) if entry["type"] != 1 else 0,
                           entry.get("size", 0))
    while len(raw) % SECTOR:
        raw += (b"\x00" * 64 + struct.pack("<HBB", 0, 0, 0)
                + struct.pack("<III", NOSTREAM, NOSTREAM, NOSTREAM)
                + b"\x00" * 36 + struct.pack("<IQ", 0, 0))
    directory_start = allocate(bytes(raw))

    # 4. The FAT, its own sectors marked FATSECT, at the end.
    data_sectors = len(sectors)
    fat_count = 1
    while data_sectors + fat_count > fat_count * (SECTOR // 4):
        fat_count += 1
    assert fat_count <= 109, "a fixture that needs a DIFAT is out of scope"
    fat_ids = list(range(data_sectors, data_sectors + fat_count))
    fat += [FATSECT] * fat_count
    fat += [FREESECT] * (fat_count * (SECTOR // 4) - len(fat))
    fat_bytes = b"".join(struct.pack("<I", v) for v in fat)
    sectors += _chunks(fat_bytes, SECTOR)

    header = bytearray(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16)
    header += struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
    header += b"\x00" * 6
    header += struct.pack("<IIIIIIIII", 0, fat_count, directory_start, 0,
                          CUTOFF, minifat_start, minifat_sectors, ENDOFCHAIN, 0)
    header += b"".join(struct.pack("<I", fat_ids[k] if k < fat_count else FREESECT)
                       for k in range(109))
    assert len(header) == SECTOR
    return bytes(header) + b"".join(sectors)


def _prop(prop: int, kind: int) -> str:
    return f"__substg1.0_{prop:04X}{kind:04X}"


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def _filetime(moment: datetime) -> int:
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return int((moment - epoch).total_seconds() * 10_000_000)


def msg_streams(*, subject="", sender="", sender_email="", to="", cc="",
                body=None, html=None, rtf_only=False, sent=None,
                attachments=()) -> dict[str, bytes]:
    """The named streams of a synthetic message. ``attachments`` are
    ``(filename, mime, data)`` triples."""
    streams: dict[str, bytes] = {}
    for prop, value in ((0x0037, subject), (0x0C1A, sender), (0x5D01, sender_email),
                        (0x0E04, to), (0x0E03, cc)):
        if value:
            streams[_prop(prop, 0x001F)] = _utf16(value)
    if body is not None:
        streams[_prop(0x1000, 0x001F)] = _utf16(body)
    if html is not None:
        streams[_prop(0x1013, 0x0102)] = html.encode("utf-8")
    if rtf_only:
        streams[_prop(0x1009, 0x0102)] = b"LZFu-not-really-rtf"
    header = struct.pack("<8sIIII8s", b"\x00" * 8, 0, len(attachments), 0,
                         len(attachments), b"\x00" * 8)
    entries = b""
    if sent is not None:
        entries += struct.pack("<IIQ", (0x0039 << 16) | 0x0040, 0, _filetime(sent))
    streams["__properties_version1.0"] = header + entries
    for k, (filename, mime, data) in enumerate(attachments):
        storage = f"__attach_version1.0_#{k:08X}"
        streams[f"{storage}/{_prop(0x3707, 0x001F)}"] = _utf16(filename)
        if mime:
            streams[f"{storage}/{_prop(0x370E, 0x001F)}"] = _utf16(mime)
        streams[f"{storage}/{_prop(0x3701, 0x0102)}"] = data
        streams[f"{storage}/__properties_version1.0"] = b"\x00" * 8
    return streams


def build_msg(**fields) -> bytes:
    return build_cfb(msg_streams(**fields))
