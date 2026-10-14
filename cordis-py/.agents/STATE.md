# Current State

Updated: 2026-10-14

## Project phase

Phase 0 through Phase 5 implementation assets exist in this directory, and `docs/roadmap.md` and
`docs/plans/README.md` record those phases as complete. The Python suite was re-verified in place after
the move from the repository root, so the Python half of those records is current evidence. The one
outstanding verification boundary is the pinned TypeScript reference oracle, described under
"Blockers" below.

## Active plan

[Plan 05: Cordis Core Equivalence](../docs/plans/05-cordis-core-equivalence.md) (recorded complete;
only the TypeScript-side reference oracle run remains outstanding).

## Verification — current (2026-10-14)

Run in a throwaway Python 3.12.13 + `uv` 0.11.5 environment created outside the repository (a
temporary directory under `/tmp`), against the checked-in `uv.lock` with `--frozen`, so no dependency
or lockfile change was introduced. No environment or cache files were written into the repository
tree; the temporary environment is not a project dependency and can be discarded.

| Command | Result |
| --- | --- |
| `uv sync --dev --frozen` | succeeded; 16 packages |
| `uv run pytest` | 76 passed, 0 failed; a repeat run produced the same result |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | 82 files already formatted |
| `uv run pyright` (1.1.411) | 0 errors, 0 warnings |
| `uv build` | succeeded; sdist and wheel built |
| `git diff --check` | passed |

Python Cordis Core oracle, `tests/compat/oracle.py … --check`: all 21 Core scenarios `004` through
`024` passed.

Not covered by this run:

- the pinned TypeScript reference oracle (see "Blockers");
- scenarios `001`–`003`: they are Harness-shaped host scenarios and `oracle.py` does not implement
  them, so they are neither passing evidence nor failures;
- the per-chapter `Tutorial/*/src/test_chapterNN.py` tests: not run in this round, so the tutorial
  chapter suite is still unverified. This does not affect the Python suite above.

## Completed work (implementation; recorded before re-verification)

These items record implementation work reported complete before the repository reorganization. The
2026-10-14 Python re-verification above now corroborates them for the Python side; the cross-language
half of the Plan 05 exit criteria is still evidence from the earlier record only, as noted below.

- [x] Implemented the lifecycle-correct Context, Registry, Reflect, Fiber, Effect, Events, Service and Logger core.
- [x] Implemented the Loader transaction runtime and representative keyless Harness profile.
- [x] Classified every vendored Cordis Core 4.0.2 public contract and documented each Python language difference.
- [x] Aligned canonical portable concepts, names, signatures, errors and lifecycle behavior.
- [x] Added Context-bound Events, Reflect, Registry and Logger service facades without duplicating backing services.
- [x] Made Fiber directly awaitable and matched validation, Effect metadata, service availability and Registry deletion contracts.
- [x] Removed redundant compatibility aliases and kept function-plugin dependency metadata in canonical `.inject` form.
- [x] Moved project-specific call-chain observation into the separate 27-line `cordis_observer` package.
- [x] Matched all 21 paired Cordis 4.0.2/Python scenarios (`004` through `024`) after normalization.
- [x] Reviewed final Core size: 2,686 Python lines versus the 1,794-line baseline and 2,693-line TypeScript reference.

## Verification — historical record (pre-reorganization, 2026-09-03)

Last run performed in the original repository layout, before the move into `cordis-py/`. Kept as
history. It is superseded for the Python side by the 2026-10-14 run above; the only difference in the
Python results is the format-check file count (81 files then, 82 now), which reflects the current tree
rather than a regression.

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed (81 files).
- `uv run pyright`: passed with zero errors and warnings.
- `uv run pytest`: passed, 76 tests.
- `uv build`: passed for sdist and wheel.
- `git diff --check`: passed.
- Twenty-one paired TypeScript/Python Core scenarios (`004` through `024`): recorded as exact
  normalized matches. That record was not produced against the pinned Cordis 4.0.2 build required
  today (commit `dd6322d604e00eec1ba5e0c8541159906a21094a`), so it must not be read as current
  cross-language evidence; only the Python half has been re-verified.

Static inventory of the current tree, independent of any test run:

- 76 `def test_…` functions under `tests/`;
- 24 behavior-scenario JSON files under `tests/compat/scenarios/` (21 Cordis Core oracles `004`–`024`
  plus 3 Harness-shaped scenarios `001`–`003`).

## Blockers

- Implementation: no known blocker. No recorded failure, unexplained capability difference or
  unresolved architectural decision is open.
- Verification of the Python implementation: no blocker remains. The suite, lint, format check, type
  check, package build and the 21-scenario Python oracle run all pass on this checkout.
- Remaining boundary: the pinned TypeScript live reference oracle is not verified. It requires the
  **built** `lib/index.js` of `@deepseek-ai/cordis` 4.0.2 at commit
  `dd6322d604e00eec1ba5e0c8541159906a21094a`; no such build exists on this machine, and another
  installed Cordis version is not valid evidence. Scenarios `001`–`003` are outside the Python oracle
  by construction (Harness-shaped host scenarios), and the Tutorial chapter tests were not run in this
  round — neither is a blocker for the Python suite.

## Next action

1. When (and only when) a build of `@deepseek-ai/cordis` 4.0.2 at commit
   `dd6322d604e00eec1ba5e0c8541159906a21094a` is available, run the paired oracle for `004`–`024` —
   `CORDIS_REFERENCE_ENTRY=/path/to/cordis/lib/index.js node tests/compat/oracle.mjs
   tests/compat/scenarios/004-core-smoke.json --check` — and record the result here. Until then, do not
   describe the scenarios as cross-language equivalence evidence.
2. Optionally run the per-chapter `Tutorial/*/src/test_chapterNN.py` tests from each chapter's `src/`
   directory and record that separate result.
3. Keep this snapshot current; no planned implementation work remains beyond these verifications. The
   repository reorganization itself is still uncommitted, which is the user's decision to make.

## Resolved decisions

- Python 3.11+ with Python 3.12 for local development;
- async-first, dynamically injected lifecycle semantics;
- vendored Cordis Core 4.0.2 behavior as the compatibility reference;
- `await fiber`/`fiber.wait()`, `Context.is_context()` and `global_` as keyword-safe Python spellings;
- one scheduler, service store, event bus and Effect implementation per capability;
- Python-only Loader, typing helpers and observer remain explicitly outside the equivalence claim.

## Repository reorganization

The Python project that used to live at the repository root was moved, unchanged, into this `cordis-py/`
directory. The move is mechanical: file contents were preserved and the tracked root copies now show as
deletions. Markdown relative links were retargeted where the move invalidated them. This reorganization
is not yet committed. The Python suite was re-verified in place after the move (2026-10-14, see above);
no functional regression from the move was observed.

## Working tree scope

Phase 0 through Phase 5 implementation assets are present in `cordis-py/`; the uncommitted changes
comprise the documented Cordis Core equivalence implementation, its verification artifacts, the
in-progress repository reorganization that moved those files here from the repository root, and this
documentation refresh. The `cordis-go/` directory remains empty and untouched.
