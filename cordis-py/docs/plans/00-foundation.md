# Plan 00: Foundation Decisions

Status: Complete

## Goal

Resolve implementation-shaping decisions and establish reproducible repository commands before writing runtime code.

## Dependencies

- Architecture chapters 01–12 reviewed;
- vendored Cordis source remains available as the behavioral reference.

## In scope

- minimum Python version;
- packaging and dependency-management tool;
- test, type-check, lint and formatting commands;
- async-first lifecycle decision;
- Context service-access API decision;
- initial package/module layout;
- scenario format for TypeScript/Python behavior comparison.

## Out of scope

- runtime implementation;
- Loader implementation;
- Harness service plugins.

## Work packages

- [x] Inventory available Python/tooling constraints.
- [x] Decide minimum Python version and document consequences.
- [x] Decide lifecycle async contract and synchronous convenience boundary.
- [x] Decide dynamic `ctx.service` versus explicit `ctx.get()` contract.
- [x] Select build, environment, test, lint and type-check tools.
- [x] Accept or replace proposed ADRs.
- [x] Write canonical commands into `AGENTS.md` and README.
- [x] Define the first cross-language behavior fixture.

## Acceptance criteria

- A clean checkout can install dependencies and run an empty test suite with documented commands.
- Public lifecycle signatures are no longer blocked by open decisions.
- Phase 1 can begin without choosing infrastructure ad hoc.
- All accepted choices have an ADR or an explicit note that they are reversible implementation details.

## Verification

- Execute install, lint, type-check and test commands from a clean environment.
- Confirm build metadata produces an importable wheel.
- Review all relative documentation links.

## Resolved decisions

- Python 3.11 minimum; Python 3.12 local development interpreter;
- uv with hatchling;
- Pyright strict mode;
- Ruff linting and formatting;
- async-first lifecycle;
- dynamic and explicit Context service access;
- no `Fiber.__await__` in the initial public API.

## Completion evidence

- `uv sync --dev` completed and produced `uv.lock`;
- Ruff lint and format checks passed;
- Pyright strict check passed;
- pytest package smoke test passed;
- source distribution and wheel build passed;
- relative Markdown links and JSON scenario syntax passed;
- first scenario: `tests/compat/scenarios/001-provider-replacement.json`.
