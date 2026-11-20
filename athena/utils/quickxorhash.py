"""Pure-Python Microsoft Graph QuickXorHash digest (160-bit, shift 11).

Matches the algorithm documented by Microsoft for OneDrive / SharePoint
``file.hashes.quickXorHash`` (base64-encoded 20-byte digest), implemented with
a 160-byte stride buffer and zero external dependencies so large files stream
in bounded RAM (~50 MiB in ~0.1 s).
"""

from __future__ import annotations

import base64

WIDTH_IN_BITS = 160
SHIFT = 11
DIGEST_BYTES = WIDTH_IN_BITS // 8  # 20 bytes
_MASK_160 = (1 << WIDTH_IN_BITS) - 1


class QuickXorHash:
    """Streaming calculator for Microsoft QuickXorHash digests."""

    def __init__(self) -> None:
        # Period of shift 11 mod 160 is 160 because gcd(11, 160) == 1.
        self._lanes = bytearray(WIDTH_IN_BITS)
        self._pos = 0
        self._length = 0

    def update(self, data: bytes) -> None:
        """Fold *data* into the running QuickXorHash state."""
        if not data:
            return
        mv = memoryview(data)
        n = len(mv)
        self._length += n
        idx = 0

        if self._pos:
            take = min(WIDTH_IN_BITS - self._pos, n)
            pos = self._pos
            for k in range(take):
                self._lanes[pos + k] ^= mv[k]
            self._pos = (pos + take) % WIDTH_IN_BITS
            idx += take

        full_end = idx + ((n - idx) // WIDTH_IN_BITS) * WIDTH_IN_BITS
        if full_end > idx:
            acc = int.from_bytes(self._lanes, "little")
            for p in range(idx, full_end, WIDTH_IN_BITS):
                acc ^= int.from_bytes(mv[p : p + WIDTH_IN_BITS], "little")
            self._lanes[:] = acc.to_bytes(WIDTH_IN_BITS, "little")
            idx = full_end

        if idx < n:
            rem = n - idx
            for k in range(rem):
                self._lanes[k] ^= mv[idx + k]
            self._pos = rem

    def digest(self) -> bytes:
        """Return the 20-byte QuickXorHash digest."""
        reg = 0
        shift = 0
        for byte_val in self._lanes:
            if byte_val:
                low = (byte_val << shift) & _MASK_160
                high = byte_val >> (WIDTH_IN_BITS - shift) if shift > 152 else 0
                reg ^= low | high
            shift += SHIFT
            if shift >= WIDTH_IN_BITS:
                shift -= WIDTH_IN_BITS
        out = bytearray(reg.to_bytes(DIGEST_BYTES, "little"))
        length_bytes = (self._length & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
        for j in range(8):
            out[DIGEST_BYTES - 8 + j] ^= length_bytes[j]
        return bytes(out)

    def b64digest(self) -> str:
        """Return the base64-encoded QuickXorHash digest."""
        return base64.b64encode(self.digest()).decode("ascii")


def quickxorhash_b64(data: bytes) -> str:
    """Compute the base64 QuickXorHash digest of *data*."""
    hasher = QuickXorHash()
    hasher.update(data)
    return hasher.b64digest()
