# G.726 stream parity — development notes

Context for reviewers. The task's own description, difficulty rationale,
solution outline, and verification strategy are in `task.toml`; this file covers
how the task is built and why it is put together the way it is.

## Layout and what the agent can see

```
environment/            the agent's image
  app/g726.py           deliberately wrong starter, frozen CLI
  app/check_public.py   public checker over the 8 short vectors
  app/fixtures/         those 8 vectors
  app/Makefile          `make check`
solution/g726.py        canonical implementation (never in the agent image)
tests/reference.py      independent reference used to derive expected bytes
tests/test_codec.py     the 19 graded checks
tests/support/          regenerates the public vectors from the reference
tests/adversarial/      mutation controls
```

The agent gets a starter that runs and produces plausible output with wrong
sample values, eight short vectors, and a checker for them. It does not get the
reference, a workload generator, or any hidden case. Only `/app/g726.py` is
collected, so the graded artifact is a standalone file rather than anything that
can reach back into the task.

## Why the public vectors are short

This is the central design decision. The eight public vectors are generated from
the reference by `tests/support/make_public.py` and are deliberately brief.
Short signals constrain the common path and say little about long-state
divergence, transition handling, or adversarial codeword histories — which is
exactly where the graded cases look.

The effect is visible in the trial results: all three codex runs got
`make check` reporting 8/8 and still scored 2/19 on the hidden verifier. An
implementation can satisfy every signal the agent can see and still be wrong.

## Verifier isolation

The verifier is a separate image. `tests/test.sh` sets `/logs/verifier` and
`/tests` to mode 700 before pytest runs, and `tests/test_codec.py` invokes the
candidate through `runuser -u nobody`, so the untrusted file executes
unprivileged and cannot reach the reward channel or read the hidden cases. The
reward is written by root from pytest's exit code after the agent container is
gone.

## Mutation controls

`tests/adversarial/run_controls.py` builds deliberately-broken variants of the
canonical solution and asserts each one fails. The mutations are single, precise
defects of the kind a plausible implementation actually produces:

| control | defect |
|---|---|
| `approximate` | audio-quality ADPCM instead of the fixed-point path |
| `floor_div` | floor division on negative values instead of C-style narrowing |
| `history_sign` | inverted predictor history sign |
| `no_transition` | transition detection disabled |
| `wrong_packing` | MSB-first ITU packing substituted for LSB-first AAL2 |

Each isolates one rule, so a pass proves the corresponding check is load-bearing
rather than incidentally satisfied.

## The runtime check

`test_06_runtime` is a guard, not a performance target. It bounds four encodes
of a 65,536-sample signal at 20 seconds total; the canonical solution takes
4.33 seconds measured on a 2-CPU container, about 4.6x under the limit. It
exists to reject a submission that re-derives tables per sample or shells out
repeatedly, and the
bound is stated in `instruction.md` so it is part of the contract. It passed in
all six standard trials, so it has never been the deciding check.

## Reproducing

Check results, trial results, and the failure analysis are in the repository
root `README.md` and `docs/FAILURE-ANALYSIS.md`, including the commands to
re-run every check and trial.
