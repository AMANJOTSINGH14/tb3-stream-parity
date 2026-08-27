#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

import g726


def main() -> int:
    vectors = json.loads(Path("/app/fixtures/public_vectors.json").read_text())
    failures = []
    for vector in vectors:
        source = bytes.fromhex(vector["input_hex"])
        expected = bytes.fromhex(vector["output_hex"])
        operation = getattr(g726, vector["operation"])
        actual = operation(source, vector["rate"], vector["packing"])
        if actual != expected:
            first = next((index for index, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1]), min(len(actual), len(expected)))
            failures.append(f"{vector['name']}: first mismatch at byte {first}; got {len(actual)} bytes, expected {len(expected)}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"public G.726 vectors passed ({len(vectors)}/{len(vectors)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
