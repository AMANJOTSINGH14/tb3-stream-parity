#!/usr/bin/env python3
"""Gateway codec: ITU-T G.726 ADPCM at 24 and 40 kbit/s.

The ADPCM core follows the fixed-point computational details of the
Recommendation (limited-width arithmetic, floating point predictor
multiplies, scale factor adaptation, tone/transition detection).
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


# --------------------------------------------------------------------------
# Fixed-point helpers
# --------------------------------------------------------------------------

_POWER2 = (1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80,
           0x100, 0x200, 0x400, 0x800, 0x1000, 0x2000, 0x4000)


def _s16(value: int) -> int:
    """Truncate to a 16-bit two's complement word."""
    return ((value + 0x8000) & 0xFFFF) - 0x8000


def _quan(value: int, table) -> int:
    """Index of the first table entry greater than ``value``."""
    for index, threshold in enumerate(table):
        if value < threshold:
            return index
    return len(table)


# --------------------------------------------------------------------------
# Per-rate quantizer tables (Tables 3 and 5 of the Recommendation)
# --------------------------------------------------------------------------

class _Rate:
    __slots__ = ("bits", "quan", "dqln", "witab", "fitab", "b_shift")

    def __init__(self, bits, quan, dqln, witab, fitab, b_shift):
        self.bits = bits
        self.quan = quan
        self.dqln = dqln
        self.witab = witab
        self.fitab = fitab
        self.b_shift = b_shift


_RATE_24000 = _Rate(
    bits=3,
    quan=(8, 218, 331),
    dqln=(-2048, 135, 273, 373, 373, 273, 135, -2048),
    witab=(-128, 960, 4384, 18624, 18624, 4384, 960, -128),
    fitab=(0, 0x200, 0x400, 0xE00, 0xE00, 0x400, 0x200, 0),
    b_shift=8,
)

_RATE_40000 = _Rate(
    bits=5,
    quan=(-122, -16, 68, 139, 198, 250, 298, 339,
          378, 413, 445, 475, 502, 528, 553),
    dqln=(-2048, -66, 28, 104, 169, 224, 274, 318,
          358, 395, 429, 459, 488, 514, 539, 566,
          566, 539, 514, 488, 459, 429, 395, 358,
          318, 274, 224, 169, 104, 28, -66, -2048),
    witab=(448, 448, 768, 1248, 1280, 1312, 1856, 3200,
           4512, 5728, 7008, 8960, 11456, 14080, 16928, 22272,
           22272, 16928, 14080, 11456, 8960, 7008, 5728, 4512,
           3200, 1856, 1312, 1280, 1248, 768, 448, 448),
    fitab=(0, 0, 0, 0, 0, 0x200, 0x200, 0x200,
           0x200, 0x200, 0x400, 0x600, 0x800, 0xA00, 0xC00, 0xC00,
           0xC00, 0xC00, 0xA00, 0x800, 0x600, 0x400, 0x200, 0x200,
           0x200, 0x200, 0x200, 0, 0, 0, 0, 0),
    b_shift=9,
)

_RATES = {24000: _RATE_24000, 40000: _RATE_40000}


# --------------------------------------------------------------------------
# ADPCM core
# --------------------------------------------------------------------------

class G726State:
    """Coder state, initialised to the Recommendation's reset values."""

    __slots__ = ("rate", "yl", "yu", "dms", "dml", "ap",
                 "a", "b", "pk", "dq", "sr", "td")

    def __init__(self, rate: _Rate):
        self.rate = rate
        self.yl = 34816
        self.yu = 544
        self.dms = 0
        self.dml = 0
        self.ap = 0
        self.a = [0, 0]
        self.b = [0, 0, 0, 0, 0, 0]
        self.pk = [0, 0]
        self.dq = [32, 32, 32, 32, 32, 32]
        self.sr = [32, 32]
        self.td = 0

    # -- FMULT ------------------------------------------------------------
    @staticmethod
    def _fmult(an: int, srn: int) -> int:
        anmag = an if an > 0 else ((-an) & 0x1FFF)
        anexp = _quan(anmag, _POWER2) - 6
        if anmag == 0:
            anmant = 32
        elif anexp >= 0:
            anmant = anmag >> anexp
        else:
            anmant = anmag << -anexp
        wanexp = anexp + ((srn >> 6) & 0xF) - 13
        wanmant = (anmant * (srn & 0o77) + 0x30) >> 4
        if wanexp >= 0:
            retval = (wanmant << wanexp) & 0x7FFF
        else:
            retval = wanmant >> -wanexp
        return -retval if (an ^ srn) < 0 else retval

    # -- ACCUM ------------------------------------------------------------
    def _predictor_zero(self) -> int:
        fmult = self._fmult
        b = self.b
        dq = self.dq
        total = fmult(b[0] >> 2, dq[0])
        for index in range(1, 6):
            total += fmult(b[index] >> 2, dq[index])
        return total

    def _predictor_pole(self) -> int:
        return (self._fmult(self.a[1] >> 2, self.sr[1])
                + self._fmult(self.a[0] >> 2, self.sr[0]))

    # -- MIX --------------------------------------------------------------
    def _step_size(self) -> int:
        if self.ap >= 256:
            return self.yu
        y = self.yl >> 6
        dif = self.yu - y
        al = self.ap >> 2
        if dif > 0:
            y += (dif * al) >> 6
        elif dif < 0:
            y += (dif * al + 0x3F) >> 6
        return y

    # -- LOG / SUBTB / QUAN -----------------------------------------------
    def _quantize(self, d: int, y: int) -> int:
        table = self.rate.quan
        size = len(table)
        dqm = abs(d)
        exp = _quan(dqm >> 1, _POWER2)
        mant = ((dqm << 7) >> exp) & 0x7F
        dl = (exp << 7) + mant
        dln = dl - (y >> 2)
        index = _quan(dln, table)
        if d < 0:
            return (size << 1) + 1 - index
        if index == 0:
            return (size << 1) + 1
        return index

    # -- ADDA / ANTILOG ---------------------------------------------------
    @staticmethod
    def _reconstruct(sign: int, dqln: int, y: int) -> int:
        dql = dqln + (y >> 2)
        if dql < 0:
            return -0x8000 if sign else 0
        dex = (dql >> 7) & 15
        dqt = 128 + (dql & 127)
        dq = (dqt << 7) >> (14 - dex)
        return dq - 0x8000 if sign else dq

    # -- adaptation -------------------------------------------------------
    def _update(self, y: int, wi: int, fi: int,
                dq: int, sr: int, dqsez: int) -> None:
        pk0 = 1 if dqsez < 0 else 0
        mag = dq & 0x7FFF

        # TRANS: transition (modem tone) detection.
        ylint = self.yl >> 15
        ylfrac = (self.yl >> 10) & 0x1F
        thr1 = _s16((32 + ylfrac) << ylint)
        thr2 = (31 << 10) if ylint > 9 else thr1
        dqthr = (thr2 + (thr2 >> 1)) >> 1
        if self.td == 0:
            tr = 0
        elif mag <= dqthr:
            tr = 0
        else:
            tr = 1

        # FUNCTW & FILTD & DELAY: unlocked scale factor.
        yu = y + ((wi - y) >> 5)
        if yu < 544:
            yu = 544
        elif yu > 5120:
            yu = 5120
        self.yu = yu

        # FILTE & DELAY: locked scale factor.
        self.yl += yu + ((-self.yl) >> 6)

        if tr == 1:
            self.a[0] = 0
            self.a[1] = 0
            self.b = [0, 0, 0, 0, 0, 0]
            a2p = 0
        else:
            pks1 = pk0 ^ self.pk[0]

            # UPA2 / LIMC: second pole coefficient.
            a2p = _s16(self.a[1] - (self.a[1] >> 7))
            if dqsez != 0:
                fa1 = self.a[0] if pks1 else _s16(-self.a[0])
                if fa1 < -8191:
                    a2p -= 0x100
                elif fa1 > 8191:
                    a2p += 0xFF
                else:
                    a2p += fa1 >> 5
                if pk0 ^ self.pk[1]:
                    if a2p <= -12160:
                        a2p = -12288
                    elif a2p >= 12416:
                        a2p = 12288
                    else:
                        a2p -= 0x80
                else:
                    if a2p <= -12416:
                        a2p = -12288
                    elif a2p >= 12160:
                        a2p = 12288
                    else:
                        a2p += 0x80
                a2p = _s16(a2p)
            self.a[1] = a2p

            # UPA1 / LIMD: first pole coefficient.
            a1 = _s16(self.a[0] - (self.a[0] >> 8))
            if dqsez != 0:
                a1 = _s16(a1 + 192) if pks1 == 0 else _s16(a1 - 192)
            a1ul = 15360 - a2p
            if a1 < -a1ul:
                a1 = -a1ul
            elif a1 > a1ul:
                a1 = a1ul
            self.a[0] = a1

            # UPB: zero coefficients.
            shift = self.rate.b_shift
            b = self.b
            nonzero = mag != 0
            for index in range(6):
                value = _s16(b[index] - (b[index] >> shift))
                if nonzero:
                    if (dq ^ self.dq[index]) >= 0:
                        value = _s16(value + 128)
                    else:
                        value = _s16(value - 128)
                b[index] = value

        # DELAY / FLOAT A: quantized difference history.
        dqs = self.dq
        dqs[5] = dqs[4]
        dqs[4] = dqs[3]
        dqs[3] = dqs[2]
        dqs[2] = dqs[1]
        dqs[1] = dqs[0]
        if mag == 0:
            dqs[0] = 0x20 if dq >= 0 else -1000
        else:
            exp = _quan(mag, _POWER2)
            value = (exp << 6) + ((mag << 6) >> exp)
            dqs[0] = value if dq >= 0 else value - 0x400

        # DELAY / FLOAT B: reconstructed signal history.
        self.sr[1] = self.sr[0]
        if sr == 0:
            self.sr[0] = 0x20
        elif sr > 0:
            exp = _quan(sr, _POWER2)
            self.sr[0] = (exp << 6) + ((sr << 6) >> exp)
        elif sr > -32768:
            srmag = -sr
            exp = _quan(srmag, _POWER2)
            self.sr[0] = (exp << 6) + ((srmag << 6) >> exp) - 0x400
        else:
            self.sr[0] = -1000

        # DELAY A: sign history.
        self.pk[1] = self.pk[0]
        self.pk[0] = pk0

        # TONE: partial band signal detection.
        if tr == 1:
            self.td = 0
        elif a2p < -11776:
            self.td = 1
        else:
            self.td = 0

        # Adaptation speed control: FILTA, FILTB, TRIGA, FILTC.
        self.dms = _s16(self.dms + ((fi - self.dms) >> 5))
        self.dml = _s16(self.dml + (((fi << 2) - self.dml) >> 7))
        if tr == 1:
            self.ap = 256
        elif y < 1536:
            self.ap += (0x200 - self.ap) >> 4
        elif self.td == 1:
            self.ap = 256
        elif abs((self.dms << 2) - self.dml) >= (self.dml >> 3):
            self.ap += (0x200 - self.ap) >> 4
        else:
            self.ap += (-self.ap) >> 4

    # -- public per-sample entry points ------------------------------------
    def encode_sample(self, sample: int) -> int:
        rate = self.rate
        sl = sample >> 2                       # 14-bit uniform PCM

        sezi = _s16(self._predictor_zero())
        sez = sezi >> 1
        sei = _s16(sezi + self._predictor_pole())
        se = sei >> 1

        d = _s16(sl - se)

        y = self._step_size()
        index = self._quantize(d, y)

        sign_bit = 1 << (rate.bits - 1)
        dq = _s16(self._reconstruct(index & sign_bit, rate.dqln[index], y))
        sr = _s16(se - (dq & 0x7FFF)) if dq < 0 else _s16(se + dq)
        dqsez = _s16(_s16(sr + sez) - se)

        self._update(y, rate.witab[index], rate.fitab[index], dq, sr, dqsez)
        return index

    def decode_code(self, index: int) -> int:
        rate = self.rate
        index &= (1 << rate.bits) - 1

        sezi = _s16(self._predictor_zero())
        sez = sezi >> 1
        sei = _s16(sezi + self._predictor_pole())
        se = sei >> 1

        y = self._step_size()
        sign_bit = 1 << (rate.bits - 1)
        dq = _s16(self._reconstruct(index & sign_bit, rate.dqln[index], y))
        sr = _s16(se - (dq & 0x7FFF)) if dq < 0 else _s16(se + dq)
        dqsez = _s16(_s16(sr + sez) - se)

        self._update(y, rate.witab[index], rate.fitab[index], dq, sr, dqsez)

        out = sr << 2                          # back to 16-bit uniform PCM
        if out > 32767:
            return 32767
        if out < -32768:
            return -32768
        return out


# --------------------------------------------------------------------------
# Bit packing
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# File level operations
# --------------------------------------------------------------------------

def _rate_of(rate: int) -> _Rate:
    try:
        return _RATES[rate]
    except KeyError:
        raise ValueError("rate must be 24000 or 40000") from None


def encode(pcm: bytes, rate: int, packing: str) -> bytes:
    if len(pcm) % 2:
        raise ValueError("PCM input length must be even")
    samples = [item[0] for item in struct.iter_unpack("<h", pcm)]
    if len(samples) % 8:
        raise ValueError("PCM sample count must be a multiple of eight")
    profile = _rate_of(rate)
    state = G726State(profile)
    codes = [state.encode_sample(sample) for sample in samples]
    return pack_codes(codes, profile.bits, packing)


def decode(payload: bytes, rate: int, packing: str) -> bytes:
    profile = _rate_of(rate)
    bits = profile.bits
    if (len(payload) * 8) % bits:
        raise ValueError("codeword payload is not sample-aligned")
    state = G726State(profile)
    samples = [state.decode_code(code)
               for code in unpack_codes(payload, bits, packing)]
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
