#!/usr/bin/env python3
"""Fixed-point ITU-T G.726 codec for the 24 and 40 kbit/s modes."""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass, field
from pathlib import Path


_POWER2 = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048,
           4096, 8192, 16384)


@dataclass(frozen=True)
class _Mode:
    bits: int
    qtab: tuple[int, ...]
    dqln: tuple[int, ...]
    wi: tuple[int, ...]
    fi: tuple[int, ...]


_MODES = {
    3: _Mode(
        3,
        (8, 218, 331),
        (-2048, 135, 273, 373, 373, 273, 135, -2048),
        (-128, 960, 4384, 18624, 18624, 4384, 960, -128),
        (0, 0x200, 0x400, 0xE00, 0xE00, 0x400, 0x200, 0),
    ),
    5: _Mode(
        5,
        (-122, -16, 68, 139, 198, 250, 298, 339, 378, 413, 445, 475,
         502, 528, 553),
        (-2048, -66, 28, 104, 169, 224, 274, 318,
         358, 395, 429, 459, 488, 514, 539, 566,
         566, 539, 514, 488, 459, 429, 395, 358,
         318, 274, 224, 169, 104, 28, -66, -2048),
        (448, 448, 768, 1248, 1280, 1312, 1856, 3200,
         4512, 5728, 7008, 8960, 11456, 14080, 16928, 22272,
         22272, 16928, 14080, 11456, 8960, 7008, 5728, 4512,
         3200, 1856, 1312, 1280, 1248, 768, 448, 448),
        (0, 0, 0, 0, 0, 0, 0x200, 0x200,
         0x200, 0x200, 0x200, 0x400, 0x600, 0x800, 0xC00, 0xC00,
         0xC00, 0xC00, 0x800, 0x600, 0x400, 0x200, 0x200, 0x200,
         0x200, 0x200, 0, 0, 0, 0, 0, 0),
    ),
}


def _quan(value: int, table: tuple[int, ...]) -> int:
    """Return the first table index whose entry is greater than value."""
    for index, threshold in enumerate(table):
        if value < threshold:
            return index
    return len(table)


def _fmult(an: int, srn: int) -> int:
    """Multiply a 14-bit coefficient by a 10-bit floating value."""
    anmag = an if an > 0 else (-an) & 0x1FFF
    anexp = _quan(anmag, _POWER2) - 6
    if anmag == 0:
        anmant = 32
    elif anexp >= 0:
        anmant = anmag >> anexp
    else:
        anmant = anmag << -anexp
    wanexp = anexp + ((srn >> 6) & 0xF) - 13
    wanmant = (anmant * (srn & 0x3F) + 0x30) >> 4
    retval = ((wanmant << wanexp) & 0x7FFF) if wanexp >= 0 else (wanmant >> -wanexp)
    return -retval if (an ^ srn) < 0 else retval


@dataclass
class G726:
    """The Recommendation's fixed-width adaptive quantizer and predictor."""

    bits: int
    yl: int = 34816
    yu: int = 544
    dms: int = 0
    dml: int = 0
    ap: int = 0
    a: list[int] = field(default_factory=lambda: [0, 0])
    b: list[int] = field(default_factory=lambda: [0] * 6)
    pk: list[int] = field(default_factory=lambda: [0, 0])
    dq: list[int] = field(default_factory=lambda: [32] * 6)
    sr: list[int] = field(default_factory=lambda: [32, 32])
    td: int = 0

    def __post_init__(self) -> None:
        self.mode = _MODES[self.bits]

    def _predictor_zero(self) -> int:
        return sum(_fmult(coefficient >> 2, sample)
                   for coefficient, sample in zip(self.b, self.dq))

    def _predictor_pole(self) -> int:
        return (_fmult(self.a[1] >> 2, self.sr[1])
                + _fmult(self.a[0] >> 2, self.sr[0]))

    def _step_size(self) -> int:
        if self.ap >= 256:
            return self.yu
        # YL carries six fractional bits.  MIX uses the rounded slow-scale
        # value and the Recommendation's sign-symmetric product rounding.
        y = (self.yl + 0x20) >> 6
        difference = self.yu - y
        al = self.ap >> 2
        if difference > 0:
            y += (difference * al + 0x10) >> 6
        else:
            y -= ((-difference * al + 0x10) >> 6)
        return y

    def _quantize(self, difference: int, y: int) -> int:
        magnitude = abs(difference)
        exponent = _quan(magnitude >> 1, _POWER2)
        mantissa = ((magnitude << 7) >> exponent) & 0x7F
        dln = (exponent << 7) + mantissa - (y >> 2)
        index = _quan(dln, self.mode.qtab)
        if difference < 0:
            return (len(self.mode.qtab) << 1) + 1 - index
        if index == 0:
            return (len(self.mode.qtab) << 1) + 1
        return index

    @staticmethod
    def _reconstruct(sign: int, dqln: int, y: int) -> int:
        dql = dqln + (y >> 2)
        if dql < 0:
            return -0x8000 if sign else 0
        dex = (dql >> 7) & 0xF
        dqt = 128 + (dql & 0x7F)
        dq = (dqt << 7) >> (14 - dex)
        # The negative result is a sign/magnitude word represented as a
        # signed 16-bit integer.  Its low 14 bits retain the magnitude.
        return dq - 0x8000 if sign else dq

    def _update(self, y: int, wi: int, fi: int, dq: int, sr: int,
                dqsez: int) -> None:
        pk0 = int(dqsez < 0)
        magnitude = dq & 0x7FFF

        # Detect a data/modem transition using the delayed partial predictor.
        ylint = self.yl >> 15
        ylfrac = (self.yl >> 10) & 0x1F
        thr1 = (32 + ylfrac) << ylint
        thr2 = (31 << 10) if ylint > 9 else thr1
        dqthr = (thr2 + (thr2 >> 1)) >> 1
        transition = int(self.td != 0 and magnitude > dqthr)

        # Fast and slow quantizer scale factors.
        self.yu = y + ((wi - y) >> 5)
        self.yu = max(544, min(5120, self.yu))
        self.yl += self.yu + ((-self.yl) >> 6)

        if transition:
            self.a[:] = (0, 0)
            self.b[:] = (0, 0, 0, 0, 0, 0)
            a2p = 0
        else:
            pks1 = pk0 ^ self.pk[0]

            # Second pole coefficient, with leakage and stability limits.
            a2p = self.a[1] - (self.a[1] >> 7)
            if dqsez != 0:
                fa1 = self.a[0] if pks1 else -self.a[0]
                if fa1 < -8191:
                    a2p -= 0x100
                elif fa1 > 8191:
                    a2p += 0xFF
                else:
                    a2p += fa1 >> 5
                if (pk0 ^ self.pk[1]) != 0:
                    if a2p <= -12160:
                        a2p = -12288
                    elif a2p >= 12416:
                        a2p = 12288
                    else:
                        a2p -= 0x80
                elif a2p <= -12416:
                    a2p = -12288
                elif a2p >= 12160:
                    a2p = 12288
                else:
                    a2p += 0x80
            self.a[1] = a2p

            # First pole coefficient and its joint stability limit.
            self.a[0] -= self.a[0] >> 8
            if dqsez != 0:
                self.a[0] += 192 if pks1 == 0 else -192
            a1ul = 15360 - a2p
            self.a[0] = max(-a1ul, min(a1ul, self.a[0]))

            # Six zero predictor coefficients with 1/256 leakage.
            for index in range(6):
                self.b[index] -= self.b[index] >> 8
                if magnitude != 0:
                    self.b[index] += 128 if (dq ^ self.dq[index]) >= 0 else -128

        # Convert the reconstructed difference and signal to the internal
        # four-bit-exponent/six-bit-mantissa floating representation.
        self.dq[1:] = self.dq[:-1]
        if magnitude == 0:
            self.dq[0] = 0x20 if dq >= 0 else -0x3E0
        else:
            exponent = _quan(magnitude, _POWER2)
            value = (exponent << 6) + ((magnitude << 6) >> exponent)
            self.dq[0] = value if dq >= 0 else value - 0x400

        self.sr[1] = self.sr[0]
        if sr == 0:
            self.sr[0] = 0x20
        elif sr > 0:
            exponent = _quan(sr, _POWER2)
            self.sr[0] = (exponent << 6) + ((sr << 6) >> exponent)
        elif sr > -32768:
            magnitude = -sr
            exponent = _quan(magnitude, _POWER2)
            self.sr[0] = ((exponent << 6) + ((magnitude << 6) >> exponent)
                          - 0x400)
        else:
            self.sr[0] = -0x3E0

        self.pk[1] = self.pk[0]
        self.pk[0] = pk0

        # Delayed tone flag and the adaptation-speed control.
        self.td = int(not transition and a2p < -11776)
        self.dms += (fi - self.dms) >> 5
        self.dml += ((fi << 2) - self.dml) >> 7
        if transition:
            self.ap = 256
        elif y < 1536 or self.td != 0:
            self.ap += (0x200 - self.ap) >> 4
        elif abs((self.dms << 2) - self.dml) >= (self.dml >> 3):
            self.ap += (0x200 - self.ap) >> 4
        else:
            self.ap += (-self.ap) >> 4

    def _process_code(self, code: int, se: int, sez: int, y: int) -> int:
        dq = self._reconstruct(code & (1 << (self.bits - 1)),
                               self.mode.dqln[code], y)
        sr = se - (dq & 0x3FFF) if dq < 0 else se + dq
        dqsez = sr + sez - se
        self._update(y, self.mode.wi[code], self.mode.fi[code], dq, sr, dqsez)
        return sr

    def encode_sample(self, sample: int) -> int:
        sl = sample >> 2
        sezi = self._predictor_zero()
        sez = sezi >> 1
        se = (sezi + self._predictor_pole()) >> 1
        y = self._step_size()
        code = self._quantize(sl - se, y)
        self._process_code(code, se, sez, y)
        return code

    def decode_code(self, code: int) -> int:
        code &= (1 << self.bits) - 1
        sezi = self._predictor_zero()
        sez = sezi >> 1
        se = (sezi + self._predictor_pole()) >> 1
        y = self._step_size()
        sr = self._process_code(code, se, sez, y)
        # The reconstructed signal is in the 14-bit internal domain.  The
        # linear PCM output stage expands it and saturates to signed 16 bits.
        return max(-32768, min(32767, sr << 2))


def pack_codes(codes: list[int], bits: int, packing: str) -> bytes:
    mask = (1 << bits) - 1
    output = bytearray()
    accumulator = 0
    available = 0
    if packing == "aal2":
        for code in codes:
            accumulator |= (code & mask) << available
            available += bits
            while available >= 8:
                output.append(accumulator & 0xFF)
                accumulator >>= 8
                available -= 8
        if available:
            output.append(accumulator & 0xFF)
    elif packing == "itu":
        for code in codes:
            accumulator = (accumulator << bits) | (code & mask)
            available += bits
            while available >= 8:
                available -= 8
                output.append((accumulator >> available) & 0xFF)
                accumulator &= (1 << available) - 1 if available else 0
        if available:
            output.append((accumulator << (8 - available)) & 0xFF)
    else:
        raise ValueError("packing must be itu or aal2")
    return bytes(output)


def unpack_codes(payload: bytes, bits: int, packing: str) -> list[int]:
    mask = (1 << bits) - 1
    output = []
    accumulator = 0
    available = 0
    if packing == "aal2":
        for byte in payload:
            accumulator |= byte << available
            available += 8
            while available >= bits:
                output.append(accumulator & mask)
                accumulator >>= bits
                available -= bits
    elif packing == "itu":
        for byte in payload:
            accumulator = (accumulator << 8) | byte
            available += 8
            while available >= bits:
                available -= bits
                output.append((accumulator >> available) & mask)
                accumulator &= (1 << available) - 1 if available else 0
    else:
        raise ValueError("packing must be itu or aal2")
    if available and accumulator:
        raise ValueError("non-zero padding bits")
    return output


def encode(pcm: bytes, rate: int, packing: str) -> bytes:
    if len(pcm) % 2:
        raise ValueError("PCM input length must be even")
    samples = [item[0] for item in struct.iter_unpack("<h", pcm)]
    if len(samples) % 8:
        raise ValueError("PCM sample count must be a multiple of eight")
    bits = rate // 8000
    state = G726(bits)
    return pack_codes([state.encode_sample(sample) for sample in samples], bits, packing)


def decode(payload: bytes, rate: int, packing: str) -> bytes:
    bits = rate // 8000
    if (len(payload) * 8) % bits:
        raise ValueError("codeword payload is not sample-aligned")
    state = G726(bits)
    samples = [state.decode_code(code) for code in unpack_codes(payload, bits, packing)]
    return struct.pack("<" + "h" * len(samples), *samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("encode", "decode"))
    parser.add_argument("--rate", required=True, type=int, choices=(24000, 40000))
    parser.add_argument("--packing", required=True, choices=("itu", "aal2"))
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = Path(args.input).read_bytes()
    result = encode(source, args.rate, args.packing) if args.operation == "encode" else decode(source, args.rate, args.packing)
    Path(args.output).write_bytes(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
