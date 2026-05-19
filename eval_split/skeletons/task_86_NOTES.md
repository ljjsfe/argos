# task_86 SC1-A — NOTES

**Score**: 1.0 ✓ (matches gold 16/16)

## Question
"Which race was Alex Yoong in when he was in track number less than 20?"

## Semantic mapping (the trick)
"track number" → `driverStandings.position` (driver's finishing position).
NOT `round`, NOT `circuitId`, NOT `raceId`.

This is the universal gap: Qwen sees "track number" and likely maps it to circuit/track entity. The actual concept is "track standings position", which is non-obvious from column names. Even `knowledge.md` doesn't define "track number".

## Helpers used
- `safe_read_csv` (× 2)
- `safe_read_json_df` (× 1)
- `set_task_context`

## Helpers NOT needed / gap candidates
- No join coercion needed (driverId already numeric in both)
- No null-handling needed
- No magnitude check needed

## Generality assertion
The semantic-mismatch failure mode (English word → wrong column) is universal across data agents — any benchmark with semi-domain terms triggers it. Helper that could compensate: deterministic column-semantic probe (currently NOT in helper lib).

## Production failure attribution
v80 score = 0.0 on this task → 100% stable failure. Likely Qwen joined on wrong column or used `round` thinking it was "track number". No helper currently bridges this gap.
