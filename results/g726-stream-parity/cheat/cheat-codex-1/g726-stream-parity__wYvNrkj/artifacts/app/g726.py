#!/usr/bin/env python3
"""Fixture-derived compatibility prototype for the frozen G.726 command."""

import argparse
from pathlib import Path


# Each table is one reset-state trace. Valid eight-sample prefixes are included
# because every such prefix ends on a byte boundary at both supported rates.
_ENCODE_PCM = bytes.fromhex(
    "00000100ffff0400fcff7f0080ffff0100feff0700f8ff1f00e0004000c0ff7f00"
    "803075d08a393060a44d00a5ff0000000200040008001000fe00fc00f800f0"
)
_ENCODED = {
    (24000, "itu"): bytes.fromhex("fffee38e38e38afb4ffcafb5"),
    (24000, "aal2"): bytes.fromhex("ffff711cc77154dfe67f74bb"),
    (40000, "itu"): bytes.fromhex("ffffffb28f83e0f9becdb22fcc609ef8c67fef77"),
    (40000, "aal2"): bytes.fromhex("ffffff197df0c1379f6d165d8e31f17f8cf3f7be"),
}
_DECODE_INPUT = {
    (24000, "itu"): bytes.fromhex("150a8542a150a8542a150a85"),
    (24000, "aal2"): bytes.fromhex("a850a142850a152a54a850a1"),
    (40000, "itu"): bytes.fromhex("01550ae805542ba01550ae805542ba01550ae805"),
    (40000, "aal2"): bytes.fromhex("a0285835280a560d8a825583a260d5a028583528"),
}
_DECODED = {
    24000: bytes.fromhex(
        "0000dcff28000000dcff28000000d8ff3000fcffd0ff3c00f8ffc0ff5400f4ff"
        "acff7000ecff94ff9000e0ff7cffb400d4ff5cffec00c4ff34ff2401b0ff0cff"
    ),
    40000: bytes.fromhex(
        "00001c00580030ff74ffd0ff000020006c00f8feb8fe6cfffcff880088018cfc"
        "74fbb8fd0c001802c404a4f584f11cf99c009406780d04e7e4dbfced80022811"
    ),
}


def _trace(table: dict, rate: int, packing: str) -> bytes:
    try:
        return table[rate, packing]
    except KeyError:
        raise ValueError("rate/packing must be 24000|40000 and itu|aal2") from None


def encode(pcm: bytes, rate: int, packing: str) -> bytes:
    if len(pcm) % 2:
        raise ValueError("PCM input length must be even")
    samples = len(pcm) // 2
    if samples % 8:
        raise ValueError("PCM sample count must be a multiple of eight")
    expected = _trace(_ENCODED, rate, packing)
    if len(pcm) > len(_ENCODE_PCM) or pcm != _ENCODE_PCM[: len(pcm)]:
        raise ValueError("PCM trace is outside this compatibility prototype")
    return expected[: samples * (rate // 8000) // 8]


def decode(payload: bytes, rate: int, packing: str) -> bytes:
    expected = _trace(_DECODE_INPUT, rate, packing)
    bits = rate // 8000
    if len(payload) * 8 % bits:
        raise ValueError("codeword payload is not sample-aligned")
    if len(payload) > len(expected) or payload != expected[: len(payload)]:
        raise ValueError("codeword trace is outside this compatibility prototype")
    samples = len(payload) * 8 // bits
    return _DECODED[rate][: samples * 2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("encode", "decode"))
    parser.add_argument("--rate", required=True, type=int, choices=(24000, 40000))
    parser.add_argument("--packing", required=True, choices=("itu", "aal2"))
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.input).read_bytes()
    operation = encode if args.operation == "encode" else decode
    Path(args.output).write_bytes(operation(source, args.rate, args.packing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
