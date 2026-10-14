# Handoff

There is no interrupted implementation task. Plans 00 through 05 are recorded complete, and the
repository reorganization that moved this project from the repository root into `cordis-py/` is in
progress (not yet committed). The Python suite has been re-verified in place after the move, so the
2026-10-14 results below are current evidence rather than history.

## Last verified (2026-10-14)

Throwaway Python 3.12.13 + `uv` 0.11.5 environment outside the repository (a temporary directory under
`/tmp`), `uv.lock` frozen with `--frozen`; the temporary environment is not a project dependency and
left the tracked tree clean.

- `uv sync --dev --frozen`: succeeded, 16 packages;
- `uv run pytest`: 76 passed, 0 failed (a repeat run produced the same result);
- `uv run ruff check .`: passed;
- `uv run ruff format --check .`: 82 files already formatted;
- `uv run pyright` 1.1.411: 0 errors, 0 warnings;
- `uv build`: sdist and wheel built;
- `git diff --check`: passed;
- Python Cordis Core oracle `--check`: 21/21 scenarios (`004`–`024`) passed.

## Remaining boundary

The only open verification item is the pinned TypeScript live reference oracle. It requires the
**built** `lib/index.js` of `@deepseek-ai/cordis` 4.0.2 at commit
`dd6322d604e00eec1ba5e0c8541159906a21094a`:

```bash
CORDIS_REFERENCE_ENTRY=/path/to/cordis/lib/index.js \
  node tests/compat/oracle.mjs tests/compat/scenarios/004-core-smoke.json --check
```

No such build exists on this machine, and a different installed Cordis version (for example 4.0.4) is
not valid evidence. Until it runs, the scenarios are Python-side evidence only, not cross-language
equivalence evidence.

Scenarios `001`–`003` are Harness-shaped host scenarios that `tests/compat/oracle.py` does not
implement: they are not failures and not passing evidence. The per-chapter
`Tutorial/*/src/test_chapterNN.py` tests were not run in this round.

## Next agent should

1. Read `AGENTS.md` in this directory (the monorepo root still has no `AGENTS.md`);
2. Read `docs/architecture/README.md`;
3. Read `.agents/STATE.md`;
4. Review `docs/compatibility.md`;
5. Confirm any requested enhancement is outside or inside Cordis core;
6. Preserve the keyless suite even if adding optional live-provider smoke tests;
7. If a pinned Cordis 4.0.2 build at the commit above becomes available, run the paired oracle over
   `004`–`024` and record the outcome in `.agents/STATE.md`; otherwise leave the boundary documented
   instead of claiming equivalence.

Core, Loader and a representative Harness-shaped profile have implementation assets and a current,
re-verified record of 76 passing tests (the static count of `def test_…` functions in `tests/` is 76;
the older "57 passing tests" figure in earlier revisions of this file is superseded). Full Harness
business packages are intentionally not ported. `docs/draft.md` is historical source material;
numbered architecture chapters are authoritative.
