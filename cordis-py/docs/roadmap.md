# Cordis-py Roadmap

This roadmap defines project phases and exit criteria. Detailed execution steps live in [docs/plans](plans/README.md); current progress lives in [.agents/STATE.md](../.agents/STATE.md).

> **Status note.** Phase 0 through Phase 5 implementation assets exist in this directory, and the
> phase table below records them as complete. The repository is mid-reorganization: the project was
> moved, unchanged, from the repository root into `cordis-py/`, and the move is not yet committed.
> The Python side has been re-verified in place on 2026-10-14 — 76 tests pass together with lint,
> format check (82 files), type check (0 errors/0 warnings), package build and `git diff --check`, and
> the 21 Cordis Core scenarios `004`–`024` pass the Python oracle `--check` run. The paired TypeScript
> reference oracle is **not** verified: it requires the built `lib/index.js` of `@deepseek-ai/cordis`
> 4.0.2 at commit `dd6322d604e00eec1ba5e0c8541159906a21094a`, which is not available here, so the
> cross-language half of the exit criteria is still open. See
> [.agents/STATE.md](../.agents/STATE.md) for exact commands and results.

## Phase 0: Foundation decisions

Goal: make implementation-shaping choices explicit before scaffolding code.

Deliverables:

- supported Python version;
- package/build/test/type-check toolchain;
- async lifecycle contract;
- dynamic Context access contract;
- initial public API and compatibility policy;
- cross-language scenario-test format.

Exit criteria:

- foundational ADRs are accepted;
- Phase 1 plan has no blocking open questions;
- repository commands are documented in `AGENTS.md`.

## Phase 1: Semantic core

Goal: implement the smallest lifecycle-correct Cordis runtime.

Deliverables:

- errors and public protocols;
- Effect/Disposable machinery;
- Context and scoped derivation;
- PluginSpec, RegistryService and PluginRuntime;
- service provide/get and Inject;
- Fiber dependency epoch and state machine;
- five event dispatch modes;
- focused and integration tests.

Exit criteria:

- plugin load order does not affect dependency-driven activation;
- provider removal unloads consumers;
- provider restoration reloads consumers with a new epoch;
- resources clean up exactly once;
- parent disposal recursively disposes children;
- full Phase 1 verification passes.

## Phase 2: Complete core

Goal: cover the full vendored Cordis core behavior needed by Harness.

Deliverables:

- Reflect accessor, set, mixin and bind;
- Service availability checks and config merging;
- configuration validation;
- Fiber update, restart and diagnostics;
- internal lifecycle events;
- LoggerService and Exporter model;
- async setup/dispose race handling and aggregate errors.

Exit criteria:

- lifecycle race matrix passes deterministically;
- update and restart match documented waterfall behavior;
- diagnostics are read-only and side-effect free;
- cross-language core scenarios pass.

## Phase 3: Configuration runtime

Goal: turn declarative configuration into a managed plugin tree.

Deliverables:

- YAML/TOML-neutral Loader model;
- Python `module:attribute` resolver;
- include, overlay and group composition;
- environment interpolation without arbitrary code execution;
- configuration update and rollback;
- optional file/module HMR.

Exit criteria:

- Loader never performs manual service dependency ordering;
- invalid updates preserve the previous valid runtime;
- include cycles and invalid plugin references fail with source locations;
- HMR follows normal Fiber cleanup paths.

## Phase 4: DeepSeek Harness compatibility

Goal: validate Cordis-py through representative Harness capability seams.

Deliverables:

- service-definition/provider/consumer examples;
- representative LLM, tools, sessions and agent-loop plugin composition;
- Python configuration profile;
- TypeScript/Python transcript or event-sequence comparisons;
- compatibility matrix and documented intentional differences.

Exit criteria:

- replacing a provider causes the expected dependent plugin cascade;
- representative Harness composition runs without manual boot ordering;
- known deviations are explicit and tested;
- project documentation supports a new contributor implementing a plugin.

## Phase 5: Cordis Core equivalence

Goal: align Cordis-py with the concepts, public API and observable behavior of vendored `@deepseek-ai/cordis` 4.0.2, except for explicit Python language differences.

Deliverables:

- source-checked Cordis Core API and behavior matrix;
- paired TypeScript/Python behavior runner and scenarios;
- aligned Context, Registry, Fiber, Effect, Events, Reflect, Service and Logger contracts;
- documented Python spellings for APIs blocked by language syntax;
- removal of redundant pre-equivalence abstractions and aliases.

Exit criteria:

- every Cordis Core public API is classified and evidenced;
- exact and equivalent items pass their cross-language contract tests;
- no unexplained capability difference remains;
- no compatibility work introduces a second runtime mechanism;
- source growth and all retained compatibility scaffolding pass a redundancy review.

## Phase status

Assets for each phase are present under `src/`, `tests/` and `docs/`, and the Python side of the
recorded completion has been re-verified on 2026-10-14 (see the status note above). The cross-language
half of the Phase 5 exit criteria — paired TypeScript/Python scenario evidence run against the pinned
Cordis 4.0.2 build — is still outstanding.

| Phase | Status | Plan |
| --- | --- | --- |
| 0. Foundation decisions | Complete; Python re-verified 2026-10-14 | [00-foundation.md](plans/00-foundation.md) |
| 1. Semantic core | Complete; Python re-verified 2026-10-14 | [01-semantic-core.md](plans/01-semantic-core.md) |
| 2. Complete core | Complete; Python re-verified 2026-10-14 | [02-complete-core.md](plans/02-complete-core.md) |
| 3. Configuration runtime | Complete; Python re-verified 2026-10-14 | [03-configuration-runtime.md](plans/03-configuration-runtime.md) |
| 4. Harness compatibility | Complete; Python re-verified 2026-10-14 | [04-harness-compatibility.md](plans/04-harness-compatibility.md) |
| 5. Cordis Core equivalence | Python re-verified 2026-10-14; TypeScript oracle pending | [05-cordis-core-equivalence.md](plans/05-cordis-core-equivalence.md) |
