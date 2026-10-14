# Cross-language behavior scenarios

This directory contains language-neutral scenarios used to compare vendored TypeScript Cordis with Cordis-py.

Each JSON file describes inputs and expected observable events. It does not encode JavaScript object layout, Python representation or implementation-private calls.

## Scenario fields

- `id`: stable scenario identifier;
- `purpose`: behavior being compared;
- `plugins`: abstract providers and consumers;
- `steps`: ordered external actions;
- `expected`: ordered observable state, activation and cleanup events;
- `invariants`: conditions that must hold throughout the scenario.

Python tests execute these expectations directly. Harness-shaped scenarios also record the exact vendored TypeScript source tests or declarations used as behavioral evidence; they compare public outcomes rather than language-specific object layouts.

## Cordis Core oracle

Plan 05 scenarios are executable by both runtimes. Run the Python side with:

```bash
uv run python tests/compat/oracle.py tests/compat/scenarios/004-core-smoke.json --check
```

The TypeScript side intentionally does not install or copy another Cordis. Build the pinned vendored repository, then point the runner at that exact output:

```bash
CORDIS_REFERENCE_ENTRY=/path/to/deepseek-harness/vendor/cordis/lib/index.js \
  node tests/compat/oracle.mjs tests/compat/scenarios/004-core-smoke.json --check
```

Both commands write one normalized JSON object to stdout. A scenario counts as equivalence evidence only when both outputs equal its `expected` object. `CORDIS_REFERENCE_ENTRY` must refer to `@deepseek-ai/cordis` 4.0.2 at commit `dd6322d604e00eec1ba5e0c8541159906a21094a`; an arbitrary installed npm version is not valid evidence.

## Current evidence status

As of 2026-10-14 the Python oracle has been run with `--check` over the 21 Cordis Core scenarios
`004`–`024` and all of them passed. The TypeScript side has **not** been run against the pinned build
described above, so no scenario currently counts as cross-language equivalence evidence; the Python
results stand on their own. Scenarios `001`–`003` are Harness-shaped host scenarios that `oracle.py`
does not implement: they are neither passing nor failing evidence for Cordis Core equivalence.
