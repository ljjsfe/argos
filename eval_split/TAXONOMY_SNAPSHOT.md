# Frozen Weakness Taxonomy Reference

**Frozen at**: 2026-05-18
**File**: `docs/QWEN_CAPABILITY_PROFILE.md`
**Last-changed SHA**: `43ad212` (feat(diagnostics): Phase 0.2 — Qwen Layer-4 (Judge) capability probe)

## Discipline

Phase 1B (new gap-filling helpers) may **only** derive helpers from the weaknesses documented in this exact SHA. No retroactive widening of taxonomy permitted during Phase 1.

If a new weakness is observed during Phase 0 SC1 or later, it MUST be:

1. Logged separately in a new `docs/QWEN_CAPABILITY_PROFILE_v2.md`
2. Treated as a new probe → re-validated independently
3. Used only for Phase 2+ work, NOT retroactively applied to Phase 1B

## How to reference in code or PR

```bash
git show 43ad212:docs/QWEN_CAPABILITY_PROFILE.md
```

Every Phase 1B helper PR must cite which weakness from this SHA it compensates.
