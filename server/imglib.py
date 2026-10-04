"""imglib.py — 与 device.c 中 image_probe 等价的最小 PNG/JPEG 头校验。
不依赖第三方库；不可解码返回 decodable=False（不抛异常给上层）。"""
from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _u16(b: bytes, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def _u32(b: bytes, i: int) -> int:
    return (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]


def probe(data: bytes) -> tuple[bool, str | None, int, int]:
    """返回 (decodable, format, width, height)。"""
    n = len(data)
    if n >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = _u32(data, 16), _u32(data, 20)
        if 0 < w <= 32768 and 0 < h <= 32768:
            return True, "png", w, h
        return False, None, 0, 0
    if n >= 4 and data[:2] == b"\xff\xd8" and data[2] == 0xFF:
        i = 2
        while i + 9 < n:
            if data[i] != 0xFF:
                return False, None, 0, 0
            m = data[i + 1]
            if m == 0xD9:
                return False, None, 0, 0
            if m == 0xD8 or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            seg = _u16(data, i + 2)
            if seg < 2:
                return False, None, 0, 0
            if ((0xC0 <= m <= 0xC3) or (0xC5 <= m <= 0xC7) or
                    (0xC9 <= m <= 0xCB) or (0xCD <= m <= 0xCF)):
                h, w = _u16(data, i + 5), _u16(data, i + 7)
                if w and h:
                    return True, "jpeg", w, h
                return False, None, 0, 0
            i += 2 + seg
    return False, None, 0, 0
