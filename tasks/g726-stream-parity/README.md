# G.726 stream parity

This task grades a single standalone Python source file against a separate,
hidden fixed-point reference.  The public vectors are intentionally short;
the verifier uses long deterministic state trajectories and non-byte-aligned
packing at 24 and 40 kbit/s.

The canonical implementation was independently checked against the pinned
FFmpeg G.726 computational path before fixture generation.  The verifier does
not install or invoke FFmpeg and contains no network-dependent test step.
Adversarial mutation controls are documented under `tests/adversarial/`.
