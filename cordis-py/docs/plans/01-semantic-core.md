# Plan 01: Semantic Core

Status: Complete

## Goal

Implement the smallest Python Cordis runtime that correctly owns resources and reacts to service dependency changes.

## Dependencies

- Plan 00 complete;
- foundational ADRs accepted;
- canonical repository commands documented.

## In scope

- public protocols and stable errors;
- Disposable and Effect;
- root/child Context, extend and isolate;
- PluginSpec normalization and RegistryService;
- Reflect provide/get and Implementation records;
- Inject normalization and dependency reverse index;
- Fiber states, epoch, activation, unload and final dispose;
- emit, bail, serial, parallel and waterfall;
- ownership-aware listeners and child plugins.

## Out of scope

- accessors and mixins;
- config schema adapters;
- update/restart persistence hooks;
- Logger exporters;
- YAML/TOML Loader and HMR;
- Harness capability implementations.

## Implementation order

- [x] Define errors, protocols and immutable records.
- [x] Implement idempotent sync/async disposal primitives.
- [x] Implement Effect collection, rollback and diagnostics metadata.
- [x] Implement Context derivation and root ownership.
- [x] Implement PluginSpec and PluginRuntime identity.
- [x] Implement service Implementation storage and isolation labels.
- [x] Implement Inject normalization and dependency notification.
- [x] Implement Fiber state machine and epoch transitions.
- [x] Implement event dispatch and Hook ownership.
- [x] Add end-to-end dependency cascade scenarios.

## Acceptance criteria

- Consumer stays PENDING before all dependencies exist.
- Consumer activates once all dependencies exist, regardless of mount order.
- Removing a provider unloads dependent Effects.
- Restoring or replacing a provider activates a new epoch.
- Same-name services in different isolation scopes do not conflict.
- Effect rollback and disposal run exactly once.
- Parent disposal recursively disposes children.
- Waterfall continues only when `next()` is called.
- Parallel dispatch waits for all listeners and aggregates failures.

## Verification

- Focused unit tests for each primitive.
- Deterministic async lifecycle tests using Event/Future controls.
- Full test, lint and type-check commands.
- At least one TypeScript/Python scenario covering provider replacement.

## Stop conditions

Pause for a decision if implementation requires changing public lifecycle signatures, Python support, Effect cleanup ordering or service-access rules established by accepted ADRs.

## Completion evidence

- `uv run ruff check .`: passed;
- `uv run ruff format --check .`: passed;
- `uv run pyright`: passed with zero errors;
- `uv run pytest`: 14 passed;
- `uv build --offline`: source distribution and wheel passed;
- provider replacement scenario is executed against `001-provider-replacement.json`;
- focused coverage includes async Effect setup/close races, cleanup aggregation, dependency replacement, isolation, duplicate providers, five dispatch modes, once ownership and plugin listener cleanup.
