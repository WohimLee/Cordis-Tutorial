# Cordis Tutorial Monorepo

This repository holds a tutorial on building a Cordis-compatible Python runtime, the Python
implementation that the tutorial evolves toward, and a deferred Go port.

| Path | Role | Status in this tree |
| --- | --- | --- |
| [`cordis-py/`](cordis-py/) | The current Python implementation of the Cordis runtime | Phase 0–5 assets present; Python suite re-verified 2026-10-14; pinned TypeScript reference oracle not verified |
| [`Tutorial/`](Tutorial/) | Step-by-step tutorial (Chinese) that builds the runtime from scratch | Chapters 00–21 all contain written chapters and chapter source; the Timer and HMR chapters are tutorial candidate contracts, not formal implementation assets |
| `cordis-go/` | A planned Go port | Deferred; this repository state contains no `cordis-go` files |

The repository is mid-reorganization: the former root Python project was moved, unchanged, into
[`cordis-py/`](cordis-py/), and the move is not yet committed. The moved Python suite has since been
re-verified in place (see [Verification state of this checkout](#verification-state-of-this-checkout)).

## cordis-py — current implementation

[`cordis-py/`](cordis-py/) is the current implementation: a behavioral Python reproduction of the
Cordis plugin runtime vendored by DeepSeek Harness. Start with:

- [`cordis-py/README.md`](cordis-py/README.md) — project overview and core object model;
- [`cordis-py/docs/architecture/README.md`](cordis-py/docs/architecture/README.md) — architecture index;
- [`cordis-py/docs/cordis-core-compatibility.md`](cordis-py/docs/cordis-core-compatibility.md) — per-API compatibility matrix;
- [`cordis-py/.agents/STATE.md`](cordis-py/.agents/STATE.md) — current progress snapshot;
- [`cordis-py/AGENTS.md`](cordis-py/AGENTS.md) — repository rules and documented commands.

Implementation status, stated precisely:

- Phases 0–5 have implementation assets in `cordis-py/src`, `cordis-py/tests` and `cordis-py/docs`;
  [`cordis-py/docs/roadmap.md`](cordis-py/docs/roadmap.md), `cordis-py/docs/plans/README.md` and the
  verification log in [`cordis-py/.agents/STATE.md`](cordis-py/.agents/STATE.md) record those phases
  as complete.
- The Python side of those records is now current evidence, not just history: a fresh run on
  2026-10-14 passed 76 tests plus lint, format check, type check, package build and
  `git diff --check`, and the 21 Cordis Core scenarios `004`–`024` passed their Python oracle
  `--check` run. The reorganization move did not break the suite.
- Scenarios `001`–`003` are Harness-shaped host scenarios that the Python oracle does not implement;
  they are neither passing evidence nor failures.
- The paired **TypeScript** live reference oracle has **not** been verified: it requires the built
  `lib/index.js` of `@deepseek-ai/cordis` 4.0.2 at commit
  `dd6322d604e00eec1ba5e0c8541159906a21094a`. No such build is available on this machine, and a
  different installed version is not valid evidence.
- Static inventory (counted from the files, independent of any test run): 76 `def test_…` functions
  under `cordis-py/tests`, and 24 behavior-scenario JSON files under
  [`cordis-py/tests/compat/scenarios`](cordis-py/tests/compat/scenarios).

## Tutorial — step-by-step chapters

[`Tutorial/README.md`](Tutorial/README.md) is the tutorial index (written in Chinese).

- Chapters 00–21 under `Tutorial/00-why-cordis` … `Tutorial/21-mini-harness` contain written Markdown
  chapters plus a self-contained `src/chapterNN.py` module and its `src/test_chapterNN.py` tests. No
  chapter-level placeholder `README.md` files remain.
- Chapter 16 is the compatibility-evidence pipeline: it does not reimplement Cordis, it decides what
  counts as cross-language evidence and records the current status (the Python oracle passes the 21
  Cordis Core scenarios `004`–`024` on its own; the pinned TypeScript 4.0.2 build has not been run;
  `001`–`003` are Harness-shaped host scenarios that do not participate in the Core verdict).
- Chapters 17–18 cover the declarative Loader (`ParsedEntry`/`Entry`, parser, resolver, mount/update/
  replace transactions, host-driven reload) and the `ConfigComposer` (mapping adapters, relative
  recursive includes, cycle detection, provenance, id-addressed overlays).
- Chapter 19 defines the Timer **candidate contract**: a manual clock, `timeout`/`interval`/
  cancellable `sleep`/`throttle`/`debounce`, handles owned by the calling Fiber's effects, and explicit
  callback-failure, cancellation and new-epoch semantics. The formal `cordis-py` tree has **no Timer
  asset at all**, so this chapter fixes behaviour a future implementation would have to satisfy; it is
  not evidence of DSH `cordis-plugin-timer` 1.1.2 equivalence.
- Chapter 20 defines the HMR **candidate contract**: the host feeds `ChangeEvent`s, a Timer debounce
  merges bursts, config-only changes update in place, module/structure changes replace a candidate
  generation, and a failing candidate keeps or restores the previous generation. The formal
  implementation only has the host-driven `ConfigReloader.start()/reload()` — no watcher, no module-cache
  refresh — so this chapter is not DSH `cordis-plugin-hmr` 1.0.15 equivalence evidence.
- Chapter 21 is the mini harness: it composes `config/*.yaml` through includes, mounts the whole tree
  through the dependency scheduler, drives a heartbeat from the manual clock, replaces the provider
  through host-driven HMR, verifies the request path, the brief `PENDING` window and rollback/recovery,
  and closes with every counter (services, fibers, entries, listeners, timer handles, watcher
  subscriptions, effects) back at zero.
- The chapter suite was run separately from the `cordis-py` verification round, chapter by chapter from
  each chapter's own `src/` directory in a throwaway Python 3.12.13 environment with
  `PYTHONDONTWRITEBYTECODE=1` and `pytest -p no:cacheprovider`: chapters 00–21 pass (184
  tests: 126 for 00–15, 30 for 16–18, 10 for 19, 10 for 20, 8 for 21). All Python files of chapters
  16–18 (6 files) and of chapters 19–21 (12 files, including `src/plugins/*.py` and `src/run.py`) also
  pass `ruff check --target-version py311 --no-cache` and `ruff format --check --no-cache`
  (run from the repository root over the file list, matching the recorded 16–18 evidence; ruff's
  isort grouping depends on that cwd, so checking a chapter's `src/` directory in isolation reports
  I001 for every chapter whose test imports its own `chapterNN` module, old and new alike).

The tutorial targets the same design that [`cordis-py/`](cordis-py/) implements, but chapter code is
independent teaching code: each chapter `src/` directory is self-contained and imports its own
`chapterNN` module.

## cordis-go — deferred

The Go port is deferred. This repository state contains no files under `cordis-go/`, and the current
change neither creates, modifies nor deletes anything there. Do not treat `cordis-go/` as an active
deliverable.

## Minimal environment and verification entry

`cordis-py` declares `requires-python = ">=3.11"` (`cordis-py/pyproject.toml`) and pins Python 3.12
for local development (`cordis-py/.python-version`). The commands in
[`cordis-py/AGENTS.md`](cordis-py/AGENTS.md#commands) are the documented entry points:

```bash
# Python 3.11+ is required; check it first.
python3 --version

cd cordis-py
uv sync --dev --frozen  # install dependencies from the checked-in uv.lock
uv run pytest           # full test suite
uv run ruff check .     # lint
uv run ruff format --check .
uv run pyright          # type check
uv build                # sdist + wheel
```

Per-chapter tutorial tests run from the chapter's own `src/` directory, because the test imports the
chapter module by plain name:

```bash
cd Tutorial/00-why-cordis/src
python3 -m pytest test_chapter00.py
```

### Static checks that need no dependencies

These read-only commands reproduce the inventory quoted above and work with any Python 3:

```bash
grep -rhE '^\s*(async )?def test_' cordis-py/tests --include='*.py' | wc -l   # 76
ls cordis-py/tests/compat/scenarios | wc -l                                   # 24
ls -d Tutorial/[0-9][0-9]-* | wc -l                                           # 22 chapters
```

### Verification state of this checkout

Last verified 2026-10-14 in a throwaway Python 3.12.13 environment with `uv` 0.11.5, using the
checked-in `uv.lock` frozen (`--frozen`). The environment lived outside the repository (a temporary
directory under `/tmp`) and left the tracked tree clean; it is not a project dependency.

| Check | Result |
| --- | --- |
| `uv sync --dev --frozen` | 16 packages installed |
| `uv run pytest` | 76 passed, 0 failed; a repeat run gave the same result |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | 82 files already formatted |
| `uv run pyright` (1.1.411) | 0 errors, 0 warnings |
| `uv build` | sdist and wheel built |
| `git diff --check` | passed |
| Python Cordis Core oracle `--check` for scenarios `004`–`024` | 21/21 passed |

Not covered by that run:

- the pinned TypeScript reference oracle (see the `cordis-py` section above) — still unverified;
- the per-chapter `Tutorial/*/src/test_chapterNN.py` tests were **not** part of that `cordis-py` round.
  They are independent teaching tests. They were run separately afterwards, chapter by chapter from each
  chapter's own `src/` directory (see
  [Tutorial — step-by-step chapters](#tutorial--step-by-step-chapters)); the `cordis-py` results above
  are unchanged by that separate run.
- Timer and HMR remain **tutorial candidate contracts**. The formal `cordis-py` tree has no Timer module,
  test or dependency, and its HMR surface is only the host-driven `ConfigReloader`; completing chapters
  19–21 does not add or verify any formal phase 0–5 conclusion, and
  [`cordis-py/.agents/STATE.md`](cordis-py/.agents/STATE.md) is unchanged.
