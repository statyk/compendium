"""
Inline-SVG QR code renderer.

Public API::

    from compendium.web.qrcode import qr_svg

    svg_string = qr_svg("https://library.example.org/ui/scan/pair?c=TOKEN")

Returns a complete ``<svg>…</svg>`` string (black modules on a white
background, no external references) suitable for direct embedding in
an HTML template via ``{{ svg | safe }}``.

The rendering layer is built on top of a vendored copy of Project
Nayuki's QR Code generator library (Python edition).  Because this
module only converts data to an SVG string it has no dependency on
secure-context availability; the caller (the phone-pairing route)
is responsible for enforcing HTTPS before calling ``qr_svg()``.

---------------------------------------------------------------------
Vendored encoding core
---------------------------------------------------------------------
Source : https://github.com/nayuki/QR-Code-generator
File   : python/qrcodegen.py  (commit on master, 2025-era)
License: MIT License
         Copyright (c) Project Nayuki.
         https://www.nayuki.io/page/qr-code-generator-library

         Permission is hereby granted, free of charge, to any person
         obtaining a copy of this software and associated documentation
         files (the "Software"), to deal in the Software without
         restriction, including without limitation the rights to use,
         copy, modify, merge, publish, distribute, sublicense, and/or
         sell copies of the Software, and to permit persons to whom the
         Software is furnished to do so, subject to the following
         conditions:
         - The above copyright notice and this permission notice shall
           be included in all copies or substantial portions of the
           Software.
         - The Software is provided "as is", without warranty of any
           kind, express or implied, including but not limited to the
           warranties of merchantability, fitness for a particular
           purpose and noninfringement. In no event shall the authors
           or copyright holders be liable for any claim, damages or
           other liability, whether in an action of contract, tort or
           otherwise, arising from, out of or in connection with the
           Software or the use or other dealings in the Software.
---------------------------------------------------------------------
"""

from __future__ import annotations

import collections
import itertools
import re
from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Vendored: Project Nayuki QR Code generator (Python)
# Source: https://github.com/nayuki/QR-Code-generator/blob/master/python/qrcodegen.py
# License: MIT (see module docstring above)
# ---------------------------------------------------------------------------


class _QrCode:
    """A QR Code symbol (Model 2, versions 1–40, all EC levels).

    Use the static factory methods:
    - ``encode_text(text, ecl)`` — Unicode string, auto-selects mode.
    - ``encode_binary(data, ecl)`` — raw bytes, always byte mode.
    """

    # ---- Static factory functions ----

    @staticmethod
    def encode_text(text: str, ecl: _QrCode.Ecc) -> _QrCode:
        segs: list[_QrSegment] = _QrSegment.make_segments(text)
        return _QrCode.encode_segments(segs, ecl)

    @staticmethod
    def encode_binary(data: bytes | Sequence[int], ecl: _QrCode.Ecc) -> _QrCode:
        return _QrCode.encode_segments([_QrSegment.make_bytes(data)], ecl)

    @staticmethod
    def encode_segments(
        segs: Sequence[_QrSegment],
        ecl: _QrCode.Ecc,
        minversion: int = 1,
        maxversion: int = 40,
        mask: int = -1,
        boostecl: bool = True,
    ) -> _QrCode:
        if not (_QrCode.MIN_VERSION <= minversion <= maxversion <= _QrCode.MAX_VERSION) or not (
            -1 <= mask <= 7
        ):
            raise ValueError("Invalid value")

        for version in range(minversion, maxversion + 1):
            datacapacitybits: int = _QrCode._get_num_data_codewords(version, ecl) * 8
            datausedbits: int | None = _QrSegment.get_total_bits(segs, version)
            if (datausedbits is not None) and (datausedbits <= datacapacitybits):
                break
            if version >= maxversion:
                msg: str = "Segment too long"
                if datausedbits is not None:
                    msg = (  # noqa: E501 — upstream diagnostic string, keep intact
                        f"Data length = {datausedbits} bits, "
                        f"Max capacity = {datacapacitybits} bits"
                    )
                raise _DataTooLongError(msg)
        assert datausedbits is not None

        for newecl in (_QrCode.Ecc.MEDIUM, _QrCode.Ecc.QUARTILE, _QrCode.Ecc.HIGH):
            if boostecl and (datausedbits <= _QrCode._get_num_data_codewords(version, newecl) * 8):
                ecl = newecl

        bb = _BitBuffer()
        for seg in segs:
            bb.append_bits(seg.get_mode().get_mode_bits(), 4)
            bb.append_bits(seg.get_num_chars(), seg.get_mode().num_char_count_bits(version))
            bb.extend(seg._bitdata)
        assert len(bb) == datausedbits

        datacapacitybits = _QrCode._get_num_data_codewords(version, ecl) * 8
        assert len(bb) <= datacapacitybits
        bb.append_bits(0, min(4, datacapacitybits - len(bb)))
        bb.append_bits(0, -len(bb) % 8)
        assert len(bb) % 8 == 0

        for padbyte in itertools.cycle((0xEC, 0x11)):
            if len(bb) >= datacapacitybits:
                break
            bb.append_bits(padbyte, 8)

        datacodewords = bytearray([0] * (len(bb) // 8))
        for i, bit in enumerate(bb):
            datacodewords[i >> 3] |= bit << (7 - (i & 7))

        return _QrCode(version, ecl, datacodewords, mask)

    # ---- Private fields (type annotations only) ----

    _version: int
    _size: int
    _errcorlvl: _QrCode.Ecc
    _mask: int
    _modules: list[list[bool]]
    _isfunction: list[list[bool]]

    # ---- Constructor ----

    def __init__(
        self,
        version: int,
        errcorlvl: _QrCode.Ecc,
        datacodewords: bytes | Sequence[int],
        msk: int,
    ) -> None:
        if not (_QrCode.MIN_VERSION <= version <= _QrCode.MAX_VERSION):
            raise ValueError("Version value out of range")
        if not (-1 <= msk <= 7):
            raise ValueError("Mask value out of range")

        self._version = version
        self._size = version * 4 + 17
        self._errcorlvl = errcorlvl

        self._modules = [[False] * self._size for _ in range(self._size)]
        self._isfunction = [[False] * self._size for _ in range(self._size)]

        self._draw_function_patterns()
        allcodewords: bytes = self._add_ecc_and_interleave(bytearray(datacodewords))
        self._draw_codewords(allcodewords)

        if msk == -1:
            minpenalty: int = 1 << 32
            for i in range(8):
                self._apply_mask(i)
                self._draw_format_bits(i)
                penalty = self._get_penalty_score()
                if penalty < minpenalty:
                    msk = i
                    minpenalty = penalty
                self._apply_mask(i)
        assert 0 <= msk <= 7
        self._mask = msk
        self._apply_mask(msk)
        self._draw_format_bits(msk)
        del self._isfunction

    # ---- Accessors ----

    def get_version(self) -> int:
        return self._version

    def get_size(self) -> int:
        return self._size

    def get_error_correction_level(self) -> _QrCode.Ecc:
        return self._errcorlvl

    def get_mask(self) -> int:
        return self._mask

    def get_module(self, x: int, y: int) -> bool:
        return (0 <= x < self._size) and (0 <= y < self._size) and self._modules[y][x]

    # ---- Private drawing helpers ----

    def _draw_function_patterns(self) -> None:
        for i in range(self._size):
            self._set_function_module(6, i, i % 2 == 0)
            self._set_function_module(i, 6, i % 2 == 0)
        self._draw_finder_pattern(3, 3)
        self._draw_finder_pattern(self._size - 4, 3)
        self._draw_finder_pattern(3, self._size - 4)
        alignpatpos: list[int] = self._get_alignment_pattern_positions()
        numalign: int = len(alignpatpos)
        skips: Sequence[tuple[int, int]] = ((0, 0), (0, numalign - 1), (numalign - 1, 0))
        for i in range(numalign):
            for j in range(numalign):
                if (i, j) not in skips:
                    self._draw_alignment_pattern(alignpatpos[i], alignpatpos[j])
        self._draw_format_bits(0)
        self._draw_version()

    def _draw_format_bits(self, mask: int) -> None:
        data: int = self._errcorlvl.formatbits << 3 | mask
        rem: int = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits: int = (data << 10 | rem) ^ 0x5412
        assert bits >> 15 == 0
        for i in range(0, 6):
            self._set_function_module(8, i, _get_bit(bits, i))
        self._set_function_module(8, 7, _get_bit(bits, 6))
        self._set_function_module(8, 8, _get_bit(bits, 7))
        self._set_function_module(7, 8, _get_bit(bits, 8))
        for i in range(9, 15):
            self._set_function_module(14 - i, 8, _get_bit(bits, i))
        for i in range(0, 8):
            self._set_function_module(self._size - 1 - i, 8, _get_bit(bits, i))
        for i in range(8, 15):
            self._set_function_module(8, self._size - 15 + i, _get_bit(bits, i))
        self._set_function_module(8, self._size - 8, True)

    def _draw_version(self) -> None:
        if self._version < 7:
            return
        rem: int = self._version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits: int = self._version << 12 | rem
        assert bits >> 18 == 0
        for i in range(18):
            bit: bool = _get_bit(bits, i)
            a: int = self._size - 11 + i % 3
            b: int = i // 3
            self._set_function_module(a, b, bit)
            self._set_function_module(b, a, bit)

    def _draw_finder_pattern(self, x: int, y: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                xx, yy = x + dx, y + dy
                if (0 <= xx < self._size) and (0 <= yy < self._size):
                    self._set_function_module(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))

    def _draw_alignment_pattern(self, x: int, y: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function_module(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)

    def _set_function_module(self, x: int, y: int, isdark: bool) -> None:
        assert type(isdark) is bool  # noqa: E721
        self._modules[y][x] = isdark
        self._isfunction[y][x] = True

    def _add_ecc_and_interleave(self, data: bytearray) -> bytes:
        version: int = self._version
        assert len(data) == _QrCode._get_num_data_codewords(version, self._errcorlvl)
        numblocks: int = _QrCode._NUM_ERROR_CORRECTION_BLOCKS[self._errcorlvl.ordinal][version]
        blockecclen: int = _QrCode._ECC_CODEWORDS_PER_BLOCK[self._errcorlvl.ordinal][version]
        rawcodewords: int = _QrCode._get_num_raw_data_modules(version) // 8
        numshortblocks: int = numblocks - rawcodewords % numblocks
        shortblocklen: int = rawcodewords // numblocks
        blocks: list[bytes] = []
        rsdiv: bytes = _QrCode._reed_solomon_compute_divisor(blockecclen)
        k: int = 0
        for i in range(numblocks):
            dat: bytearray = data[
                k: k + shortblocklen - blockecclen + (0 if i < numshortblocks else 1)
            ]
            k += len(dat)
            ecc: bytes = _QrCode._reed_solomon_compute_remainder(dat, rsdiv)
            if i < numshortblocks:
                dat.append(0)
            blocks.append(dat + ecc)
        assert k == len(data)
        result = bytearray()
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if (i != shortblocklen - blockecclen) or (j >= numshortblocks):
                    result.append(blk[i])
        assert len(result) == rawcodewords
        return result

    def _draw_codewords(self, data: bytes) -> None:
        assert len(data) == _QrCode._get_num_raw_data_modules(self._version) // 8
        i: int = 0
        for right in range(self._size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(self._size):
                for j in range(2):
                    x: int = right - j
                    upward: bool = (right + 1) & 2 == 0
                    y: int = (self._size - 1 - vert) if upward else vert
                    if (not self._isfunction[y][x]) and (i < len(data) * 8):
                        self._modules[y][x] = _get_bit(data[i >> 3], 7 - (i & 7))
                        i += 1
        assert i == len(data) * 8

    def _apply_mask(self, mask: int) -> None:
        if not (0 <= mask <= 7):
            raise ValueError("Mask value out of range")
        masker: collections.abc.Callable[[int, int], int] = _QrCode._MASK_PATTERNS[mask]
        for y in range(self._size):
            for x in range(self._size):
                self._modules[y][x] ^= (masker(x, y) == 0) and (not self._isfunction[y][x])

    def _get_penalty_score(self) -> int:
        result: int = 0
        size: int = self._size
        modules: list[list[bool]] = self._modules
        for y in range(size):
            runcolor: bool = False
            runx: int = 0
            runhistory: collections.deque[int] = collections.deque([0] * 7, 7)
            for x in range(size):
                if modules[y][x] == runcolor:
                    runx += 1
                    if runx == 5:
                        result += _QrCode._PENALTY_N1
                    elif runx > 5:
                        result += 1
                else:
                    self._finder_penalty_add_history(runx, runhistory)
                    if not runcolor:
                        result += (
                            self._finder_penalty_count_patterns(runhistory) * _QrCode._PENALTY_N3
                        )
                    runcolor = modules[y][x]
                    runx = 1
            result += (
                self._finder_penalty_terminate_and_count(runcolor, runx, runhistory)
                * _QrCode._PENALTY_N3
            )
        for x in range(size):
            runcolor = False
            runy: int = 0
            runhistory = collections.deque([0] * 7, 7)
            for y in range(size):
                if modules[y][x] == runcolor:
                    runy += 1
                    if runy == 5:
                        result += _QrCode._PENALTY_N1
                    elif runy > 5:
                        result += 1
                else:
                    self._finder_penalty_add_history(runy, runhistory)
                    if not runcolor:
                        result += (
                            self._finder_penalty_count_patterns(runhistory) * _QrCode._PENALTY_N3
                        )
                    runcolor = modules[y][x]
                    runy = 1
            result += (
                self._finder_penalty_terminate_and_count(runcolor, runy, runhistory)
                * _QrCode._PENALTY_N3
            )
        for y in range(size - 1):
            for x in range(size - 1):
                if (
                    modules[y][x]
                    == modules[y][x + 1]
                    == modules[y + 1][x]
                    == modules[y + 1][x + 1]
                ):
                    result += _QrCode._PENALTY_N2
        dark: int = sum((1 if cell else 0) for row in modules for cell in row)
        total: int = size**2
        k: int = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        assert 0 <= k <= 9
        result += k * _QrCode._PENALTY_N4
        return result

    def _get_alignment_pattern_positions(self) -> list[int]:
        if self._version == 1:
            return []
        numalign: int = self._version // 7 + 2
        step: int = (self._version * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
        result: list[int] = [
            (self._size - 7 - i * step) for i in range(numalign - 1)
        ] + [6]
        return list(reversed(result))

    @staticmethod
    def _get_num_raw_data_modules(ver: int) -> int:
        if not (_QrCode.MIN_VERSION <= ver <= _QrCode.MAX_VERSION):
            raise ValueError("Version number out of range")
        result: int = (16 * ver + 128) * ver + 64
        if ver >= 2:
            numalign: int = ver // 7 + 2
            result -= (25 * numalign - 10) * numalign - 55
            if ver >= 7:
                result -= 36
        assert 208 <= result <= 29648
        return result

    @staticmethod
    def _get_num_data_codewords(ver: int, ecl: _QrCode.Ecc) -> int:
        return (
            _QrCode._get_num_raw_data_modules(ver) // 8
            - _QrCode._ECC_CODEWORDS_PER_BLOCK[ecl.ordinal][ver]
            * _QrCode._NUM_ERROR_CORRECTION_BLOCKS[ecl.ordinal][ver]
        )

    @staticmethod
    def _reed_solomon_compute_divisor(degree: int) -> bytes:
        if not (1 <= degree <= 255):
            raise ValueError("Degree out of range")
        result = bytearray([0] * (degree - 1) + [1])
        root: int = 1
        for _ in range(degree):
            for j in range(degree):
                result[j] = _QrCode._reed_solomon_multiply(result[j], root)
                if j + 1 < degree:
                    result[j] ^= result[j + 1]
            root = _QrCode._reed_solomon_multiply(root, 0x02)
        return result

    @staticmethod
    def _reed_solomon_compute_remainder(data: bytes, divisor: bytes) -> bytes:
        result = bytearray([0] * len(divisor))
        for b in data:
            factor: int = b ^ result.pop(0)
            result.append(0)
            for i, coef in enumerate(divisor):
                result[i] ^= _QrCode._reed_solomon_multiply(coef, factor)
        return result

    @staticmethod
    def _reed_solomon_multiply(x: int, y: int) -> int:
        if (x >> 8 != 0) or (y >> 8 != 0):
            raise ValueError("Byte out of range")
        z: int = 0
        for i in reversed(range(8)):
            z = (z << 1) ^ ((z >> 7) * 0x11D)
            z ^= ((y >> i) & 1) * x
        assert z >> 8 == 0
        return z

    def _finder_penalty_count_patterns(self, runhistory: collections.deque[int]) -> int:
        n: int = runhistory[1]
        assert n <= self._size * 3
        core: bool = (
            n > 0
            and (runhistory[2] == runhistory[4] == runhistory[5] == n)
            and runhistory[3] == n * 3
        )
        return (
            1 if (core and runhistory[0] >= n * 4 and runhistory[6] >= n) else 0
        ) + (1 if (core and runhistory[6] >= n * 4 and runhistory[0] >= n) else 0)

    def _finder_penalty_terminate_and_count(
        self,
        currentruncolor: bool,
        currentrunlength: int,
        runhistory: collections.deque[int],
    ) -> int:
        if currentruncolor:
            self._finder_penalty_add_history(currentrunlength, runhistory)
            currentrunlength = 0
        currentrunlength += self._size
        self._finder_penalty_add_history(currentrunlength, runhistory)
        return self._finder_penalty_count_patterns(runhistory)

    def _finder_penalty_add_history(
        self, currentrunlength: int, runhistory: collections.deque[int]
    ) -> None:
        if runhistory[0] == 0:
            currentrunlength += self._size
        runhistory.appendleft(currentrunlength)

    # ---- Constants ----

    MIN_VERSION: int = 1
    MAX_VERSION: int = 40

    _PENALTY_N1: int = 3
    _PENALTY_N2: int = 3
    _PENALTY_N3: int = 40
    _PENALTY_N4: int = 10

    _ECC_CODEWORDS_PER_BLOCK: Sequence[Sequence[int]] = (
        # fmt: off
        # 0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40  # noqa: E501
        (-1,  7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # Low  # noqa: E501
        (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),  # Medium  # noqa: E501
        (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # Quartile  # noqa: E501
        (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # High  # noqa: E501
        # fmt: on
    )

    _NUM_ERROR_CORRECTION_BLOCKS: Sequence[Sequence[int]] = (
        # fmt: off
        # 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40  # noqa: E501
        (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4,  4,  4,  4,  4,  6,  6,  6,  6,  7,  8,  8,  9,  9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),  # Low  # noqa: E501
        (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5,  5,  8,  9,  9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),  # Medium  # noqa: E501
        (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8,  8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),  # Quartile  # noqa: E501
        (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),  # High  # noqa: E501
        # fmt: on
    )

    _MASK_PATTERNS: Sequence[collections.abc.Callable[[int, int], int]] = (
        (lambda x, y: (x + y) % 2),
        (lambda x, y: y % 2),
        (lambda x, y: x % 3),
        (lambda x, y: (x + y) % 3),
        (lambda x, y: (x // 3 + y // 2) % 2),
        (lambda x, y: x * y % 2 + x * y % 3),
        (lambda x, y: (x * y % 2 + x * y % 3) % 2),
        (lambda x, y: ((x + y) % 2 + x * y % 3) % 2),
    )

    class Ecc:
        """Error-correction level."""

        ordinal: int
        formatbits: int

        def __init__(self, i: int, fb: int) -> None:
            self.ordinal = i
            self.formatbits = fb

        LOW: _QrCode.Ecc
        MEDIUM: _QrCode.Ecc
        QUARTILE: _QrCode.Ecc
        HIGH: _QrCode.Ecc

    Ecc.LOW = Ecc(0, 1)
    Ecc.MEDIUM = Ecc(1, 0)
    Ecc.QUARTILE = Ecc(2, 3)
    Ecc.HIGH = Ecc(3, 2)


# ---------------------------------------------------------------------------


class _QrSegment:
    """A segment of data in a QR Code (byte mode, numeric, alphanumeric, etc.)."""

    @staticmethod
    def make_bytes(data: bytes | Sequence[int]) -> _QrSegment:
        bb = _BitBuffer()
        for b in data:
            bb.append_bits(b, 8)
        return _QrSegment(_QrSegment.Mode.BYTE, len(data), bb)

    @staticmethod
    def make_numeric(digits: str) -> _QrSegment:
        if not _QrSegment.is_numeric(digits):
            raise ValueError("String contains non-numeric characters")
        bb = _BitBuffer()
        i = 0
        while i < len(digits):
            n = min(len(digits) - i, 3)
            bb.append_bits(int(digits[i: i + n]), n * 3 + 1)
            i += n
        return _QrSegment(_QrSegment.Mode.NUMERIC, len(digits), bb)

    @staticmethod
    def make_alphanumeric(text: str) -> _QrSegment:
        if not _QrSegment.is_alphanumeric(text):
            raise ValueError("String contains unencodable characters in alphanumeric mode")
        bb = _BitBuffer()
        for i in range(0, len(text) - 1, 2):
            temp = _QrSegment._ALPHANUMERIC_ENCODING_TABLE[text[i]] * 45
            temp += _QrSegment._ALPHANUMERIC_ENCODING_TABLE[text[i + 1]]
            bb.append_bits(temp, 11)
        if len(text) % 2 > 0:
            bb.append_bits(_QrSegment._ALPHANUMERIC_ENCODING_TABLE[text[-1]], 6)
        return _QrSegment(_QrSegment.Mode.ALPHANUMERIC, len(text), bb)

    @staticmethod
    def make_segments(text: str) -> list[_QrSegment]:
        if text == "":
            return []
        elif _QrSegment.is_numeric(text):
            return [_QrSegment.make_numeric(text)]
        elif _QrSegment.is_alphanumeric(text):
            return [_QrSegment.make_alphanumeric(text)]
        else:
            return [_QrSegment.make_bytes(text.encode("UTF-8"))]

    @staticmethod
    def is_numeric(text: str) -> bool:
        return _QrSegment._NUMERIC_REGEX.fullmatch(text) is not None

    @staticmethod
    def is_alphanumeric(text: str) -> bool:
        return _QrSegment._ALPHANUMERIC_REGEX.fullmatch(text) is not None

    _mode: _QrSegment.Mode
    _numchars: int
    _bitdata: list[int]

    def __init__(
        self, mode: _QrSegment.Mode, numch: int, bitdata: Sequence[int]
    ) -> None:
        if numch < 0:
            raise ValueError()
        self._mode = mode
        self._numchars = numch
        self._bitdata = list(bitdata)

    def get_mode(self) -> _QrSegment.Mode:
        return self._mode

    def get_num_chars(self) -> int:
        return self._numchars

    def get_data(self) -> list[int]:
        return list(self._bitdata)

    @staticmethod
    def get_total_bits(segs: Sequence[_QrSegment], version: int) -> int | None:
        result = 0
        for seg in segs:
            ccbits: int = seg.get_mode().num_char_count_bits(version)
            if seg.get_num_chars() >= (1 << ccbits):
                return None
            result += 4 + ccbits + len(seg._bitdata)
        return result

    _NUMERIC_REGEX: re.Pattern[str] = re.compile(r"[0-9]*")
    _ALPHANUMERIC_REGEX: re.Pattern[str] = re.compile(r"[A-Z0-9 $%*+./:-]*")
    _ALPHANUMERIC_ENCODING_TABLE: dict[str, int] = {
        ch: i for i, ch in enumerate("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:")
    }

    class Mode:
        """Segment mode (numeric, alphanumeric, byte, kanji, ECI)."""

        _modebits: int
        _charcounts: tuple[int, int, int]

        def __init__(self, modebits: int, charcounts: tuple[int, int, int]):
            self._modebits = modebits
            self._charcounts = charcounts

        def get_mode_bits(self) -> int:
            return self._modebits

        def num_char_count_bits(self, ver: int) -> int:
            return self._charcounts[(ver + 7) // 17]

        NUMERIC: _QrSegment.Mode
        ALPHANUMERIC: _QrSegment.Mode
        BYTE: _QrSegment.Mode
        KANJI: _QrSegment.Mode
        ECI: _QrSegment.Mode

    Mode.NUMERIC = Mode(0x1, (10, 12, 14))
    Mode.ALPHANUMERIC = Mode(0x2, (9, 11, 13))
    Mode.BYTE = Mode(0x4, (8, 16, 16))
    Mode.KANJI = Mode(0x8, (8, 10, 12))
    Mode.ECI = Mode(0x7, (0, 0, 0))


# ---------------------------------------------------------------------------


class _BitBuffer(list[int]):
    """Appendable bit sequence used during QR encoding."""

    def append_bits(self, val: int, n: int) -> None:
        if (n < 0) or (val >> n != 0):
            raise ValueError("Value out of range")
        self.extend(((val >> i) & 1) for i in reversed(range(n)))


def _get_bit(x: int, i: int) -> bool:
    return (x >> i) & 1 != 0


class _DataTooLongError(ValueError):
    """Raised when the data does not fit any QR Code version."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_EC_MAP: dict[str, _QrCode.Ecc] = {
    "L": _QrCode.Ecc.LOW,
    "M": _QrCode.Ecc.MEDIUM,
    "Q": _QrCode.Ecc.QUARTILE,
    "H": _QrCode.Ecc.HIGH,
}


def qr_svg(data: str, *, border: int = 4, error_correction: str = "M") -> str:
    """Encode *data* as a QR Code and return a self-contained ``<svg>`` string.

    Parameters
    ----------
    data:
        The text to encode (typically a short HTTPS URL).  The encoder
        selects the smallest QR version that fits; at EC level M it
        handles up to 77 bytes for version 5 and 134 bytes for version 6,
        comfortably covering the ~120-char phone-pairing claim URL.
    border:
        Number of quiet-zone modules surrounding the symbol (default 4,
        the minimum required by the QR spec).
    error_correction:
        One of ``"L"`` (~7 %), ``"M"`` (~15 %, default), ``"Q"`` (~25 %),
        ``"H"`` (~30 %).

    Returns
    -------
    str
        A complete ``<svg xmlns="http://www.w3.org/2000/svg" ...>…</svg>``
        string with a white background and black modules.  Embed directly
        in HTML via ``{{ svg | safe }}``; no external references.

    Raises
    ------
    ValueError
        If *border* is negative, *error_correction* is not one of the
        four recognised letters, or *data* is too long for any QR version.
    """
    if border < 0:
        raise ValueError("border must be non-negative")
    ecl = _EC_MAP.get(error_correction.upper())
    if ecl is None:
        raise ValueError(f"error_correction must be one of L/M/Q/H, got {error_correction!r}")

    qr = _QrCode.encode_text(data, ecl)
    size = qr.get_size()
    total = size + border * 2

    parts: list[str] = []
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                parts.append(f"M{x + border},{y + border}h1v1h-1z")

    path = " ".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1"'
        f' viewBox="0 0 {total} {total}" stroke="none">'
        f'<rect width="100%" height="100%" fill="#FFFFFF"/>'
        f'<path d="{path}" fill="#000000"/>'
        f"</svg>"
    )
