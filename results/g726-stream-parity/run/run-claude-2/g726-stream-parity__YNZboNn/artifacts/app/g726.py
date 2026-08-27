#!/usr/bin/env python3
"""ITU-T G.726 ADPCM gateway codec at 24 and 40 kbit/s (fixed point)."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

_POWER2 = (1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80,
           0x100, 0x200, 0x400, 0x800, 0x1000, 0x2000, 0x4000)


def _short(value: int) -> int:
    """Truncate to the 16-bit two's complement width used by the reference."""
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _quan(value: int, table) -> int:
    """Return the index of the first table entry greater than value."""
    index = 0
    for entry in table:
        if value < entry:
            break
        index += 1
    return index


def _fmult(an: int, srn: int) -> int:
    """Product of a 14-bit integer and a (4-bit exponent, 6-bit mantissa) float."""
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


class _Rate:
    """Per-rate quantizer tables and widths."""

    def __init__(self, code_size: int, qtab, dqlntab, witab, fitab, dqmask: int):
        self.code_size = code_size
        self.qtab = qtab
        self.dqlntab = dqlntab
        self.witab = witab
        self.fitab = fitab
        self.dqmask = dqmask
        self.sign_bit = 1 << (code_size - 1)


_RATE_24 = _Rate(
    3,
    (8, 218, 331),
    (-2048, 135, 273, 373, 373, 273, 135, -2048),
    (-128, 960, 4384, 18624, 18624, 4384, 960, -128),
    (0, 0x200, 0x400, 0xE00, 0xE00, 0x400, 0x200, 0),
    0x3FFF,
)

_RATE_40 = _Rate(
    5,
    (-122, -16, 68, 139, 198, 250, 298, 339,
     378, 413, 445, 475, 502, 528, 553),
    (-2048, -66, 28, 104, 169, 224, 274, 318,
     359, 395, 429, 459, 488, 514, 539, 566,
     566, 539, 514, 488, 459, 429, 395, 359,
     318, 274, 224, 169, 104, 28, -66, -2048),
    (448, 448, 768, 1248, 1280, 1312, 1856, 3200,
     4512, 5728, 7008, 8960, 11456, 14080, 16928, 22272,
     22272, 16928, 14080, 11456, 8960, 7008, 5728, 4512,
     3200, 1856, 1312, 1280, 1248, 768, 448, 448),
    (0, 0, 0, 0, 0, 0x200, 0x200, 0x200,
     0x200, 0x200, 0x400, 0x600, 0x800, 0xA00, 0xC00, 0xC00,
     0xC00, 0xC00, 0xA00, 0x800, 0x600, 0x400, 0x200, 0x200,
     0x200, 0x200, 0x200, 0, 0, 0, 0, 0),
    0x7FFF,
)

_RATES = {24000: _RATE_24, 40000: _RATE_40}


class ADPCMState:
    """G.726 coder state; reset to the values mandated by the Recommendation."""

    def __init__(self, rate: _Rate):
        self.rate = rate
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

    # --- prediction -----------------------------------------------------
    def predictor_zero(self) -> int:
        sezi = _fmult(self.b[0] >> 2, self.dq[0])
        for index in range(1, 6):
            sezi += _fmult(self.b[index] >> 2, self.dq[index])
        return sezi

    def predictor_pole(self) -> int:
        return _fmult(self.a[1] >> 2, self.sr[1]) + _fmult(self.a[0] >> 2, self.sr[0])

    def step_size(self) -> int:
        """MIX: blend the locked and unlocked step size multipliers."""
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

    # --- quantizer ------------------------------------------------------
    def quantize(self, d: int, y: int) -> int:
        """Log-domain quantization of the difference signal into a codeword."""
        table = self.rate.qtab
        size = len(table)
        dqm = _short(abs(d))
        exp = _quan(dqm >> 1, _POWER2)                  # LOG
        mant = ((dqm << 7) >> exp) & 0x7F
        dl = (exp << 7) + mant
        dln = dl - (y >> 2)                             # SUBTB
        index = _quan(dln, table)                       # QUAN
        if d < 0:
            return (size << 1) + 1 - index
        if index == 0:
            return (size << 1) + 1
        return index

    def reconstruct(self, sign: int, dqln: int, y: int) -> int:
        """ADDA/ANTILOG: quantized difference in sign-magnitude form."""
        dql = dqln + (y >> 2)
        if dql < 0:
            return -0x8000 if sign else 0
        dex = (dql >> 7) & 15
        dqt = 128 + (dql & 127)
        dq = _short((dqt << 7) >> (14 - dex))
        return _short(dq - 0x8000) if sign else dq

    # --- adaptation -----------------------------------------------------
    def update(self, y: int, wi: int, fi: int, dq: int, sr: int, dqsez: int) -> None:
        code_size = self.rate.code_size
        pk0 = 1 if dqsez < 0 else 0
        mag = dq & 0x7FFF
        a2p = 0

        # TRANS: transition (modem tone) detection.
        ylint = self.yl >> 15
        ylfrac = (self.yl >> 10) & 0x1F
        thr1 = (32 + ylfrac) << ylint
        thr2 = (31 << 10) if ylint > 9 else thr1
        dqthr = (thr2 + (thr2 >> 1)) >> 1
        if self.td == 0:
            tr = 0
        elif mag <= dqthr:
            tr = 0
        else:
            tr = 1

        # FUNCTW & FILTD & DELAY, then LIMB.
        self.yu = _short(y + ((wi - y) >> 5))
        if self.yu < 544:
            self.yu = 544
        elif self.yu > 5120:
            self.yu = 5120

        # FILTE & DELAY.
        self.yl += self.yu + ((-self.yl) >> 6)

        if tr == 1:
            self.a[0] = 0
            self.a[1] = 0
            self.b = [0] * 6
        else:
            pks1 = pk0 ^ self.pk[0]                     # UPA2

            a2p = _short(self.a[1] - (self.a[1] >> 7))
            if dqsez != 0:
                fa1 = _short(self.a[0] if pks1 else -self.a[0])
                if fa1 < -8191:
                    a2p -= 0x100
                elif fa1 > 8191:
                    a2p += 0xFF
                else:
                    a2p += fa1 >> 5

                if pk0 ^ self.pk[1]:                    # LIMC
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
                a2p = _short(a2p)

            self.a[1] = a2p                             # TRIGB & DELAY

            self.a[0] = _short(self.a[0] - (self.a[0] >> 8))    # UPA1
            if dqsez != 0:
                if pks1 == 0:
                    self.a[0] = _short(self.a[0] + 192)
                else:
                    self.a[0] = _short(self.a[0] - 192)

            a1ul = _short(15360 - a2p)                  # LIMD
            if self.a[0] < -a1ul:
                self.a[0] = _short(-a1ul)
            elif self.a[0] > a1ul:
                self.a[0] = a1ul

            for index in range(6):                      # UPB
                if code_size == 5:
                    self.b[index] = _short(self.b[index] - (self.b[index] >> 9))
                else:
                    self.b[index] = _short(self.b[index] - (self.b[index] >> 8))
                if dq & 0x7FFF:                         # XOR
                    if (dq ^ self.dq[index]) >= 0:
                        self.b[index] = _short(self.b[index] + 128)
                    else:
                        self.b[index] = _short(self.b[index] - 128)

        self.dq[1:] = self.dq[:5]
        if mag == 0:                                    # FLOAT A
            self.dq[0] = 0x20 if dq >= 0 else -992
        else:
            exp = _quan(mag, _POWER2)
            value = (exp << 6) + ((mag << 6) >> exp)
            self.dq[0] = _short(value if dq >= 0 else value - 0x400)

        self.sr[1] = self.sr[0]                         # FLOAT B
        if sr == 0:
            self.sr[0] = 0x20
        elif sr > 0:
            exp = _quan(sr, _POWER2)
            self.sr[0] = _short((exp << 6) + ((sr << 6) >> exp))
        elif sr > -32768:
            mag_sr = -sr
            exp = _quan(mag_sr, _POWER2)
            self.sr[0] = _short((exp << 6) + ((mag_sr << 6) >> exp) - 0x400)
        else:
            self.sr[0] = -992

        self.pk[1] = self.pk[0]                         # DELAY A
        self.pk[0] = pk0

        if tr == 1:                                     # TONE
            self.td = 0
        elif a2p < -11776:
            self.td = 1
        else:
            self.td = 0

        self.dms = _short(self.dms + ((fi - self.dms) >> 5))            # FILTA
        self.dml = _short(self.dml + (((fi << 2) - self.dml) >> 7))     # FILTB

        if tr == 1:                                     # SUBTC & FILTC
            self.ap = 256
        elif y < 1536:
            self.ap = _short(self.ap + ((0x200 - self.ap) >> 4))
        elif self.td == 1:
            self.ap = _short(self.ap + ((0x200 - self.ap) >> 4))
        elif abs((self.dms << 2) - self.dml) >= (self.dml >> 3):
            self.ap = _short(self.ap + ((0x200 - self.ap) >> 4))
        else:
            self.ap = _short(self.ap + ((-self.ap) >> 4))

    # --- sample interface -----------------------------------------------
    def encode_sample(self, sample: int) -> int:
        rate = self.rate
        sl = sample >> 2                                # 14-bit dynamic range
        sezi = _short(self.predictor_zero())
        sez = sezi >> 1
        sei = _short(sezi + self.predictor_pole())
        se = sei >> 1                                   # signal estimate
        d = _short(sl - se)                             # SUBTA

        y = self.step_size()
        i = self.quantize(d, y)
        dq = self.reconstruct(i & rate.sign_bit, rate.dqlntab[i], y)

        sr = _short(se - (dq & rate.dqmask)) if dq < 0 else _short(se + dq)
        dqsez = _short(sr + sez - se)

        self.update(y, rate.witab[i], rate.fitab[i], dq, sr, dqsez)
        return i

    def decode_code(self, code: int) -> int:
        rate = self.rate
        i = code & ((1 << rate.code_size) - 1)
        sezi = _short(self.predictor_zero())
        sez = sezi >> 1
        sei = _short(sezi + self.predictor_pole())
        se = sei >> 1

        y = self.step_size()
        dq = self.reconstruct(i & rate.sign_bit, rate.dqlntab[i], y)

        sr = _short(se - (dq & rate.dqmask)) if dq < 0 else _short(se + dq)
        dqsez = _short(sr - se + sez)

        self.update(y, rate.witab[i], rate.fitab[i], dq, sr, dqsez)
        return max(-32768, min(32767, sr << 2))         # back to 16-bit linear


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
    state = ADPCMState(_RATES[rate])
    return pack_codes([state.encode_sample(sample) for sample in samples], bits, packing)


def decode(payload: bytes, rate: int, packing: str) -> bytes:
    bits = rate // 8000
    if (len(payload) * 8) % bits:
        raise ValueError("codeword payload is not sample-aligned")
    state = ADPCMState(_RATES[rate])
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
