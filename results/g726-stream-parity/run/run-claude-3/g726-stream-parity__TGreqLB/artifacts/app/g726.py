#!/usr/bin/env python3
"""Incomplete gateway codec. The framing is sound; the ADPCM core is not."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


class ApproximateADPCM:
    """A legacy audio-quality approximation incorrectly used for G.726."""

    def __init__(self, bits: int):
        self.bits = bits
        self.predictor = 0
        self.step = 544

    def encode_sample(self, sample: int) -> int:
        difference = sample - self.predictor
        sign = int(difference < 0)
        magnitude = abs(difference)
        levels = 1 << (self.bits - 1)
        index = min(levels - 1, magnitude * levels // max(self.step, 1))
        code = index | (sign << (self.bits - 1))
        self.decode_code(code)
        return code

    def decode_code(self, code: int) -> int:
        sign = code >> (self.bits - 1)
        index = code & ((1 << (self.bits - 1)) - 1)
        levels = 1 << (self.bits - 1)
        delta = (2 * index + 1) * self.step // (2 * levels)
        self.predictor += -delta if sign else delta
        self.predictor = max(-32768, min(32767, self.predictor))
        self.step += (index * 48 - self.step // 16)
        self.step = max(127, min(24576, self.step))
        return self.predictor


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
    state = ApproximateADPCM(bits)
    return pack_codes([state.encode_sample(sample) for sample in samples], bits, packing)


def decode(payload: bytes, rate: int, packing: str) -> bytes:
    bits = rate // 8000
    if (len(payload) * 8) % bits:
        raise ValueError("codeword payload is not sample-aligned")
    state = ApproximateADPCM(bits)
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
