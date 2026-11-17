# Bench declaration (freeze before running)

**Name / date / base ref (commit):**
**Question** (one sentence, falsifiable):
**Hypotheses and expected direction/magnitude:**

## Arms
| Arm | Code ref or knob | What differs from BASE |
|-----|------------------|------------------------|

## Cells
Plant models (exact, a+-20 %, b+-20 %), emitters (delay/inertia), sensors (resolution, cadence, heartbeat, reporting threshold), scenarios (steady, setpoint steps, morning comfort step, sun, restart...), modes (HEAT decides, COOL reported), config variants (switch, valve, quantized). Stage 1 (screening) vs stage 2 (confirmation).

## Seeds and noise floor
Seeds: ...; noise floor measured between seeds on: ... (no tolerance below twice this floor).

## Rules (never relaxed after results)
- **A strict:** ...
- **B comfort:** overshoot (not judged under 0.15 C; limit = ref + 0.10 C), steady error (not judged under 0.05 C; limit = ref + 0.03 C), failure if > 10 % of groups violate or one group > 2x tolerance.
- **C false value:** effect on command (about 10 power points).
- Reported only: COOL, slow/coarse-sensor cells, no-demand runs.

## Verdict logic
Mechanical mapping from rule results to ADOPT / REJECT / UNDETERMINED (add two seeds).

## Implementation notes (added before the freeze)
Knobs, worktrees, evaluator, commands, expected duration. Freeze hash/commit:
