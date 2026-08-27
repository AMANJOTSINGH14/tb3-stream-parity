#!/usr/bin/env python3
"""ITU-T G.726 24 and 40 kbit/s linear-PCM codec.

The arithmetic below follows the fixed-point computational description in
G.726.  In particular, ``dq`` and ``sr`` history entries use the
Recommendation's small floating-point representation; they are not PCM
samples.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


_POWER2 = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048,
           4096, 8192, 16384)


def _saturate16(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


# Quantizer decision levels (log domain), inverse-quantizer levels, scale
# adaptation, and speed-control adaptation for the two modes.
_TABLES = {
    3: (
        (8, 218, 331),
        (-2048, 135, 273, 373, 373, 273, 135, -2048),
        (-128, 960, 4384, 18624, 18624, 4384, 960, -128),
        (0, 0x200, 0x400, 0xE00, 0xE00, 0x400, 0x200, 0),
    ),
    5: (
        (-122, -16, 68, 139, 198, 250, 298, 339,
         378, 413, 445, 475, 502, 528, 553),
        (-2048, -66, 28, 104, 169, 224, 274, 318,
         358, 395, 429, 459, 488, 514, 539, 566,
         566, 539, 514, 488, 459, 429, 395, 358,
         318, 274, 224, 169, 104, 28, -66, -2048),
        (448, 448, 768, 1248, 1280, 1312, 1856, 3200,
         4512, 5728, 7008, 8960, 11456, 14080, 16928, 22272,
         22272, 16928, 14080, 11456, 8960, 7008, 5728, 4512,
         3200, 1856, 1312, 1280, 1248, 768, 448, 448),
        (0, 0, 0, 0, 0, 0, 0x200, 0x200,
         0x200, 0x200, 0x400, 0x600, 0x800, 0xC00, 0xC00, 0xC00,
         0xC00, 0xC00, 0xC00, 0x800, 0x600, 0x400, 0x200, 0x200,
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
    """Multiply a predictor coefficient by an internal Float11 value.

    The coefficient is narrowed before it is converted to floating point.
    Moving that shift after the conversion changes negative products.
    """
    coefficient = an >> 2
    negative = coefficient < 0
    magnitude = -coefficient if negative else coefficient
    exponent = _quan(magnitude, _POWER2)
    mantissa = (magnitude << 6) >> exponent if magnitude else 0

    product_exponent = exponent + ((srn >> 6) & 0xF) - 19
    product_mantissa = (mantissa * (srn & 0x3F) + 0x30) >> 4
    if product_exponent >= 0:
        value = (product_mantissa << product_exponent) & 0x7FFF
    else:
        value = product_mantissa >> -product_exponent
    return -value if negative != (srn < 0) else value


class G726:
    """One reset G.726 encoder/decoder state."""

    def __init__(self, bits: int):
        if bits not in _TABLES:
            raise ValueError("only 3-bit and 5-bit G.726 are supported")
        self.bits = bits
        self.qtab, self.dqlntab, self.witab, self.fitab = _TABLES[bits]

        # Recommendation reset state.
        self.yl = 34816
        self.yu = 544
        self.dms = 0
        self.dml = 0
        self.ap = 0
        self.a = [0, 0]
        self.b = [0] * 6
        self.pk = [0, 0]
        self.dq = [32] * 6
        self.sr = [32, 32]
        self.td = 0

    def _predictor_zero(self) -> int:
        return sum(_fmult(coef, signal)
                   for coef, signal in zip(self.b, self.dq))

    def _predictor_pole(self) -> int:
        return _fmult(self.a[1], self.sr[1]) + _fmult(self.a[0], self.sr[0])

    def _step_size(self) -> int:
        # YL has six fractional bits.  Preserve them until the interpolation
        # is complete; reducing YL first loses state that affects codewords.
        al = 64 if self.ap >= 256 else self.ap >> 2
        difference = (self.yu << 6) - self.yl
        product = difference * al
        interpolation = (product >> 6 if difference >= 0
                         else (product + 0x3F) >> 6)
        return (self.yl + interpolation + 0x10) >> 6

    def _quantize(self, difference: int, y: int) -> int:
        magnitude = -difference if difference < 0 else difference
        exponent = _quan(magnitude >> 1, _POWER2)
        mantissa = ((magnitude << 7) >> exponent) & 0x7F
        dl = (exponent << 7) + mantissa
        index = _quan(dl - (y >> 2), self.qtab)
        if difference < 0:
            return (len(self.qtab) << 1) + 1 - index
        if index == 0:
            return (len(self.qtab) << 1) + 1
        return index

    @staticmethod
    def _reconstruct(sign: bool, dqln: int, y: int) -> int:
        dql = dqln + (y >> 2)
        if dql < 0:
            return -0x8000 if sign else 0
        exponent = (dql >> 7) & 0xF
        mantissa = 128 + (dql & 0x7F)
        magnitude = (mantissa << exponent) >> 7
        return magnitude - 0x8000 if sign else magnitude

    def _update(self, y: int, wi: int, fi: int, dq: int,
                sr: int, dqsez: int) -> None:
        pk0 = int(dqsez < 0)
        magnitude = dq & 0x7FFF

        # Transition detector.
        ylint = self.yl >> 15
        ylfrac = (self.yl >> 10) & 0x1F
        thr1 = (32 + ylfrac) << ylint
        thr2 = (31 << 10) if ylint > 9 else thr1
        dqthr = (thr2 + (thr2 >> 1)) >> 1
        tr = int(self.td != 0 and magnitude > dqthr)

        # Fast and slow quantizer scale factors.
        self.yu = y + ((wi - y) >> 5)
        if self.yu < 544:
            self.yu = 544
        elif self.yu > 5120:
            self.yu = 5120
        self.yl += self.yu + ((-self.yl) >> 6)

        if tr:
            self.a[0] = self.a[1] = 0
            self.b[:] = [0] * 6
            a2p = 0
        else:
            # Second pole predictor coefficient.
            pks1 = pk0 ^ self.pk[0]
            a2p = self.a[1] - (self.a[1] >> 7)
            if dqsez != 0:
                fa1 = self.a[0] if pks1 else -self.a[0]
                if fa1 < -8191:
                    a2p -= 0x100
                elif fa1 > 8191:
                    a2p += 0xFF
                else:
                    a2p += fa1 >> 5
                if pk0 ^ self.pk[1]:
                    if a2p <= -12160:
                        a2p = -12288
                    else:
                        a2p -= 0x80
                elif a2p >= 12031:
                    a2p = 12288
                else:
                    a2p += 0x80
            self.a[1] = a2p

            # First pole predictor coefficient, limited by the second.
            self.a[0] -= self.a[0] >> 8
            if dqsez != 0:
                self.a[0] += -192 if pks1 else 192
            a1ul = 15360 - a2p
            if self.a[0] < -a1ul:
                self.a[0] = -a1ul
            elif self.a[0] > a1ul:
                self.a[0] = a1ul

            # Zero predictor coefficients.
            leak_shift = 8
            if magnitude != 0:
                for index in range(6):
                    self.b[index] -= self.b[index] >> leak_shift
                    self.b[index] += (128 if (dq < 0) == (self.dq[index] < 0)
                                      else -128)
            else:
                for index in range(6):
                    self.b[index] -= self.b[index] >> leak_shift

        # Delay the quantized difference and store its 4-bit-exponent,
        # 6-bit-mantissa floating representation.
        self.dq[1:] = self.dq[:-1]
        if magnitude == 0:
            self.dq[0] = -0x3E0 if dq < 0 else 0x20
        else:
            exponent = _quan(magnitude, _POWER2)
            value = (exponent << 6) + ((magnitude << 6) >> exponent)
            self.dq[0] = value - 0x400 if dq < 0 else value

        # Reconstructed-signal floating history.  The signal presented to
        # the Float11 converter is the saturated 16-bit reconstruction.
        sr = _saturate16(sr)
        self.sr[1] = self.sr[0]
        if sr == 0:
            self.sr[0] = 0x20
        elif sr > 0:
            exponent = _quan(sr, _POWER2)
            self.sr[0] = (exponent << 6) + ((sr << 6) >> exponent)
        elif sr > -32768:
            magnitude_sr = -sr
            exponent = _quan(magnitude_sr, _POWER2)
            self.sr[0] = ((exponent << 6) +
                          ((magnitude_sr << 6) >> exponent) - 0x400)
        else:
            self.sr[0] = -0x3E0

        self.pk[1] = self.pk[0]
        self.pk[0] = pk0

        if tr:
            self.td = 0
        else:
            self.td = int(a2p < -11776)

        self.dms += (fi - self.dms) >> 5
        self.dml += ((fi << 2) - self.dml) >> 7
        if tr:
            self.ap = 256
        elif (y < 1536 or self.td or
              abs((self.dms << 2) - self.dml) >= (self.dml >> 3)):
            self.ap += (0x200 - self.ap) >> 4
        else:
            self.ap += (-self.ap) >> 4

    def _decode_internal(self, code: int) -> int:
        sezi = self._predictor_zero()
        sez = sezi >> 1
        sei = sezi + self._predictor_pole()
        se = sei >> 1
        y = self._step_size()
        dq = self._reconstruct(bool(code & (1 << (self.bits - 1))),
                               self.dqlntab[code], y)
        sr = se - (dq & 0x3FFF) if dq < 0 else se + dq
        dqsez = sr + sez - se
        self._update(y, self.witab[code], self.fitab[code], dq, sr, dqsez)
        return sr

    def encode_sample(self, sample: int) -> int:
        sl = sample >> 2
        sezi = self._predictor_zero()
        sei = sezi + self._predictor_pole()
        se = sei >> 1
        y = self._step_size()
        code = self._quantize(sl - se, y)
        dq = self._reconstruct(bool(code & (1 << (self.bits - 1))),
                               self.dqlntab[code], y)
        sr = se - (dq & 0x3FFF) if dq < 0 else se + dq
        dqsez = sr + (sezi >> 1) - se
        self._update(y, self.witab[code], self.fitab[code], dq, sr, dqsez)
        return code

    def decode_code(self, code: int) -> int:
        return _saturate16(self._decode_internal(code) << 2)


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
