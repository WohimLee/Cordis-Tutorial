# AGENTS.md

## Project

Cordis-py is a behavioral Python reproduction of the Cordis runtime vendored by DeepSeek Harness. Preserve observable lifecycle semantics instead of mechanically translating TypeScript language features.

Read [docs/architecture/README.md](docs/architecture/README.md) before implementation work. Read the active plan and [.agents/STATE.md](.agents/STATE.md) before changing code.

## Current phase

Phase 0 through Phase 5 implementation assets exist in this directory, and `.agents/STATE.md`,
`docs/roadmap.md` and `docs/plans/README.md` record those phases as complete. The repository is
mid-reorganization: the former root Python project was moved, unchanged, to this `cordis-py/`
directory of the monorepo, and the move is not yet committed. The Python suite was re-verified in
place after the move (2026-10-14): `uv sync --dev --frozen` installed 16 packages, `uv run pytest`
passed 76 tests with 0 failures, `uv run ruff check .`, `uv run ruff format --check .` (82 files),
`uv run pyright` (0 errors, 0 warnings), `uv build` and `git diff --check` all passed, and the 21
Cordis Core scenarios `004`–`024` passed the Python oracle `--check` run. Do not treat architecture
examples as newly verified APIs beyond that run.

Not covered: the pinned TypeScript reference oracle, which needs the built `lib/index.js` of
`@deepseek-ai/cordis` 4.0.2 at commit `dd6322d604e00eec1ba5e0c8541159906a21094a` (scenarios `001`–`003`
are Harness-shaped host scenarios the Python oracle does not implement), and the per-chapter `Tutorial`
tests, which were not run. The current phase and next action are authoritative in `.agents/STATE.md`.

## Source of truth

When documents disagree, use this precedence:

1. Explicit user instruction;
2. `AGENTS.md` repository rules;
3. Accepted ADRs in `docs/decisions/`;
4. Architecture documents in `docs/architecture/`;
5. Active implementation plan in `docs/plans/`;
6. `.agents/STATE.md` progress snapshot;
7. `docs/draft.md` historical discussion.

The vendored TypeScript Cordis observable behavior is the compatibility reference. Do not copy JavaScript-only mechanisms when Python has a clearer equivalent.

## Architecture invariants

- Every long-lived resource has a Fiber owner and a lifecycle-managed disposer.
- A disposed Fiber never reactivates.
- A plugin activates only when every declared injected service is available.
- Losing an injected service unloads the consumer; restoring dependencies activates a new epoch.
- Services, listeners, child Fibers, tasks and user resources use the same Effect ownership model.
- Cleanup is idempotent, fully awaited and does not stop after the first error.
- Service resolution includes the service name and isolation label.
- Loader code never replaces the core dependency scheduler with manual topological ordering.
- User callbacks are not executed while holding a global runtime lock.

## Implementation workflow

Before starting a planned phase:

1. Read the corresponding file in `docs/plans/`;
2. Resolve or explicitly defer its open decisions;
3. Update `.agents/STATE.md` with the active task;
4. Implement the smallest acceptance slice with tests;
5. Run the checks appropriate to the changed surface;
6. Update the plan checklist and state snapshot;
7. Create or update an ADR when a durable architectural choice is made.

Do not mark an item complete merely because code exists. Its acceptance criteria and required verification must pass.

## Testing requirements

- Every behavioral change includes focused tests.
- Fiber, Effect, service notification or task-management changes include lifecycle tests.
- Async race tests use controllable `asyncio.Event` or `Future`, not timing sleeps.
- Cross-language compatibility claims require a behavior scenario derived from vendored Cordis.
- Failure tests verify both the raised error and the final lifecycle/resource state.
- Run focused tests first; run the full suite before completing a phase.

## Commands

- Install/sync: `uv sync --dev --frozen`
- Unit tests: `uv run pytest`
- Focused tests: `uv run pytest <path> -k <pattern>`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Format: `uv run ruff format .`
- Type check: `uv run pyright`
- Package build: `uv build`
- Python compat oracle (one scenario): `uv run python tests/compat/oracle.py tests/compat/scenarios/004-core-smoke.json --check`
- TypeScript compat oracle (needs the pinned Cordis 4.0.2 build): `CORDIS_REFERENCE_ENTRY=/path/to/cordis/lib/index.js node tests/compat/oracle.mjs tests/compat/scenarios/004-core-smoke.json --check`

The project supports Python 3.11 and newer; `.python-version` selects Python 3.12 for local development.

## Documentation rules

- Architecture describes the target system, not daily progress.
- Roadmap describes phases and exit criteria.
- Plans describe implementation order, scope and verification.
- `.agents/STATE.md` is a short current snapshot, not an append-only log.
- ADRs record durable decisions and consequences.
- Update affected documentation in the same change as behavior.

## Agent continuation rules

Continue autonomously when the next action is inside an accepted plan and does not change a proposed architecture decision. Stop and ask when:

- an unresolved choice changes the public API or supported Python versions;
- implementation evidence contradicts an accepted ADR;
- completing the task requires expanding scope into a later phase;
- compatibility requires choosing between TypeScript behavior and an intentionally different Python contract;
- a destructive or externally visible action was not explicitly authorized.

## Repository hygiene

- Preserve unrelated user changes.
- Do not commit generated caches, virtual environments or secrets.
- Keep files focused; do not merge architecture, plans and live state into one document.
- Files end with one newline and Markdown links remain relative inside repository documents.
