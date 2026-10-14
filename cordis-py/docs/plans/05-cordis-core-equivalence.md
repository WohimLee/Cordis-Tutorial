# Plan 05: Cordis Core Equivalence

Status: Complete

## Goal

Make Cordis-py conceptually, publicly and behaviorally equivalent to `@deepseek-ai/cordis` 4.0.2, except for explicit Python language differences, without duplicating runtime machinery or growing unnecessary abstractions.

## Compatibility boundary

In scope is the public and observable behavior of:

- `vendor/cordis/src/context.ts`;
- `vendor/cordis/src/events.ts`;
- `vendor/cordis/src/fiber.ts`;
- `vendor/cordis/src/logger.ts`;
- `vendor/cordis/src/reflect.ts`;
- `vendor/cordis/src/registry.ts`;
- `vendor/cordis/src/service.ts`;
- public utilities exported by `vendor/cordis/src/index.ts` when they represent a portable capability.

Out of scope:

- DeepSeek Harness business services and plugins;
- `cordis-plugin-loader`, `cordis-plugin-include`, `cordis-plugin-hmr` and `cordis-plugin-timer`;
- byte-identical JavaScript stack traces or log rendering;
- reimplementing Proxy, Symbol or prototype mechanics that have no independent observable contract;
- preserving pre-1.0 Cordis-py names when doing so requires a permanent parallel API.

## Constraints

- Vendored Cordis behavior is authoritative over current Python behavior and architecture examples.
- Public names match Cordis whenever valid and usable in Python.
- Every unavoidable difference is listed with rationale and paired behavior evidence.
- Existing lifecycle invariants and deterministic race tests remain valid.
- No second dependency scheduler, event bus, service store or Effect hierarchy is introduced.
- Compatibility aliases are trivial forwarding properties or methods and have a removal decision.
- Prefer deleting or consolidating superseded code while implementing each slice.

## Work packages

### 1. Compatibility inventory and oracle

- [x] Record every Cordis Core export and public member in a checked compatibility matrix.
- [x] Classify each item as exact, equivalent, language-specific, missing or Python extension.
- [x] Record signatures, return contracts, errors and observable events, not only names.
- [x] Add a minimal TypeScript runner that executes the vendored Cordis reference.
- [x] Run shared JSON scenarios against TypeScript and Python and compare normalized output.
- [x] Establish an initial source-size and public-surface baseline for later review.

### 2. Public vocabulary and object shape

- [x] Align portable class, field and method names with Cordis.
- [x] Decide the single Python spelling for `Fiber.await`; `Context.is_context()` and `global_` are accepted Python spellings.
- [x] Align `Fiber` public fields including `ctx`, config, state, store and transition visibility.
- [x] Align exported names such as `LoggerLevel`, `Message`, `Inject` and `isBailed` where practical.
- [x] Remove redundant pre-equivalence aliases after migration tests and documentation are updated.

### 3. Context and Registry

- [x] Implement `Context.extend(meta)` with inherited runtime scope and child metadata.
- [x] Implement strict and non-strict service lookup.
- [x] Define the portable Context identity and event-filter contracts.
- [x] Implement `ctx.inject(deps, callback)` through normal plugin registration.
- [x] Match function, constructor and `{ apply }` plugin normalization.
- [x] Match inherited Inject metadata and method-level Inject behavior using a Python equivalent.
- [x] Complete Registry inspection and map-like operations without duplicating storage.

### 4. Effect and Fiber contracts

- [x] Make the value returned by `ctx.effect()` provide Cordis-equivalent disposal and setup-wait semantics.
- [x] Use one public Effect implementation for synchronous setup, asynchronous setup and nested cleanup results; internal synchronous collection only preserves immediate registration semantics.
- [x] Match accepted and rejected Effect result shapes where portable.
- [x] Compare all Fiber state transitions, update/restart/dispose results and failure paths.
- [x] Match public error types and timing while retaining Python `ExceptionGroup` where it is the faithful aggregate equivalent.
- [x] Re-run the deterministic setup/dispose and dependency-change race matrix.

### 5. Events

- [x] Support dispatch context as the Python equivalent of explicit `thisArg`.
- [x] Apply Context filters and make global listeners bypass them.
- [x] Match prepend, once and listener-disposer results.
- [x] Allow `internal/listener` to replace normal registration.
- [x] Match `internal/dispatch` arguments and dispatch-mode reporting.
- [x] Match synchronous and asynchronous listener behavior for all five modes.
- [x] Support portable non-string event keys; focused tests cover object identity keys.

### 6. Reflect and Service

- [x] Match provide/get/set/notify ordering, strictness and interception hooks.
- [x] Match accessor and mixin binding behavior.
- [x] Provide Python equivalents for service availability, invocation, initialization and extension.
- [x] Preserve caller-Context tracing for service method calls.
- [x] Support custom service config merging as well as default shallow merging.
- [x] Verify isolation-label resolution across providers and consumers.

### 7. Logger

- [x] Align public `Logger`, `LoggerService`, `LoggerLevel`, `Message` and Exporter contracts.
- [x] Match named-level resolution, buffering, metadata and Fiber association.
- [x] Match portable formatter and maximum-length behavior.
- [x] Provide terminal color behavior without reproducing JavaScript-only internals.
- [x] Keep the core logger single-purpose and move the project-specific call-chain observer to `cordis_observer`.

### 8. Consolidation and release gate

- [x] Remove obsolete wrappers, duplicate names and compatibility-only branches.
- [x] Review core source growth against the baseline and justify every material increase.
- [x] Publish the final API matrix and language-difference table.
- [x] Run all paired behavior scenarios and the full Python verification suite.
- [x] Confirm no unclassified Cordis Core public API or observable behavior remains.

## Acceptance criteria

- Every Cordis Core public export and member has a matrix classification and evidence.
- Items classified exact expose the same portable name, arguments, result and error behavior.
- Items classified equivalent pass the same normalized TypeScript/Python scenario.
- Every language-specific difference explains why direct parity is impossible and preserves the capability where possible.
- Provider replacement, dependency loss, update, disposal and races retain their existing lifecycle guarantees.
- No duplicated scheduler, service store, event bus or sync/async Effect implementation exists.
- Core source growth is reviewed for redundancy; unexplained compatibility scaffolding blocks completion.
- Full lint, format, type-check, tests and package build pass.

## Verification

- generated or source-checked API inventory;
- TypeScript reference runner using the pinned vendored Cordis;
- shared JSON behavior scenarios;
- focused Python unit and lifecycle race tests;
- `uv run ruff check .`;
- `uv run ruff format --check .`;
- `uv run pyright`;
- `uv run pytest`;
- `uv build`.

## Resolved decisions

- `Fiber` is directly awaitable and retains `wait()`; `Context.is_context()` and `global_` are the documented keyword-safe spellings.
- Portable runtime utilities are exported from the package root; JavaScript-only shape and stack helpers are not reproduced.
- Redundant pre-equivalence aliases were removed instead of maintaining a parallel API.
- The final 2,686-line Core is reviewed against the 1,794-line baseline and 2,693-line TypeScript reference; retained growth implements classified contracts without duplicate runtime mechanisms.
