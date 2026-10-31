"""Recognize static PNG/WebP containers without allocating decoded pixels.

Only small base64 ranges holding chunk headers are read. Pixel data and large
metadata chunks are skipped, not decoded; original image bytes stay unchanged.
Animation controls always keep the input on the isolated decoder path.
"""
from __future__ import annotations

import base64

MAX_PIXELS = 40_000_000
MAX_CHUNKS = 4096


class Headers:
    def __init__(self, value):
        self.value = value
        self.start = value.find(",", 0, 128) + 1 if value.startswith("data:") else 0
        length = len(value) - self.start
        if length % 4:
            raise ValueError("Invalid image encoding")
        self.size = length // 4 * 3 - (2 if value.endswith("==") else 1 if value.endswith("=") else 0)

    def read(self, offset, count):
        if offset < 0 or offset + count > self.size:
            raise ValueError("Truncated image container")
        begin, end = offset // 3 * 4, (offset + count + 2) // 3 * 4
        block = base64.b64decode(self.value[self.start + begin:self.start + end], validate=True)
        result = block[offset % 3:offset % 3 + count]
        if len(result) != count:
            raise ValueError("Truncated image header")
        return result


def dimensions(width, height):
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise ValueError("Image exceeds pixel budget")


def static_image(value: str) -> bool:
    """True only for a recognized static container; uncertain layouts use isolation."""
    headers = Headers(value)
    if headers.size < 12:
        return False
    signature = headers.read(0, 12)
    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        head = headers.read(8, 25)
        if head[:8] != b"\x00\x00\x00\rIHDR":
            raise ValueError("Invalid PNG header")
        dimensions(int.from_bytes(head[8:12], "big"), int.from_bytes(head[12:16], "big"))
        offset, seen_data = 33, False
        for _ in range(MAX_CHUNKS):
            chunk = headers.read(offset, 8)
            length, kind = int.from_bytes(chunk[:4], "big"), chunk[4:8]
            if offset + length + 12 > headers.size:
                raise ValueError("Truncated PNG chunk")
            if kind in (b"acTL", b"fcTL", b"fdAT"):
                return False
            if kind == b"IEND":
                return length == 0 and seen_data
            if kind == b"IDAT":
                seen_data = True
            offset += length + 12
        return False
    if signature[:4] != b"RIFF" or signature[8:] != b"WEBP":
        return False
    end = int.from_bytes(signature[4:8], "little") + 8
    if end > headers.size or end < 20:
        raise ValueError("Invalid WebP container length")
    offset, seen_image = 12, False
    for _ in range(MAX_CHUNKS):
        if offset == end:
            return seen_image
        if offset + 8 > end:
            raise ValueError("Truncated WebP chunk")
        chunk = headers.read(offset, 8)
        kind, length = chunk[:4], int.from_bytes(chunk[4:], "little")
        if offset + 8 + length + (length & 1) > end:
            raise ValueError("Truncated WebP chunk")
        if kind in (b"ANIM", b"ANMF"):
            return False
        if kind == b"VP8X":
            if length != 10:
                return False
            head = headers.read(offset + 8, 10)
            dimensions(1 + int.from_bytes(head[4:7], "little"), 1 + int.from_bytes(head[7:10], "little"))
            if head[0] & 2:
                return False
        elif kind == b"VP8L":
            if length < 5:
                return False
            head = headers.read(offset + 8, 5)
            if head[0] != 0x2f:
                return False
            packed = int.from_bytes(head[1:5], "little")
            dimensions(1 + (packed & 0x3fff), 1 + ((packed >> 14) & 0x3fff))
            seen_image = True
        elif kind == b"VP8 ":
            if length < 10:
                return False
            head = headers.read(offset + 8, 10)
            if head[3:6] != b"\x9d\x01\x2a":
                return False
            dimensions(int.from_bytes(head[6:8], "little") & 0x3fff,
                       int.from_bytes(head[8:10], "little") & 0x3fff)
            seen_image = True
        offset += 8 + length + (length & 1)
    return False
