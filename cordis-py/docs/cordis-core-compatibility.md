# Cordis Core Compatibility Matrix

This matrix tracks Cordis-py's equivalence to `@deepseek-ai/cordis` 4.0.2 from DeepSeek Harness commit `dd6322d604e00eec1ba5e0c8541159906a21094a`.

Only `vendor/cordis/src` is in scope. Harness packages and the separate Loader, Include, HMR and Timer packages are excluded.

## Status vocabulary

- **Exact**: portable name, arguments, return contract, errors and observable behavior match.
- **Equivalent**: Python spelling or mechanism differs for a documented language reason, while behavior matches through a paired scenario.
- **Partial**: useful behavior exists but at least one public contract differs.
- **Missing**: no corresponding public capability exists.
- **Language-specific**: the declaration exposes a TypeScript/JavaScript mechanism with no independent portable runtime contract.
- **Extension**: Cordis-py behavior outside the upstream contract.

An entry is accepted as Exact or Equivalent only with paired TypeScript/Python evidence or focused evidence for a Python-only spelling constraint.

## Baseline

| Measure | Value | Notes |
| --- | ---: | --- |
| Vendored Cordis Core source | 2,693 lines | Nine `src/*.ts` files, including JSDoc and type-only declarations. |
| Cordis-py Core source | 1,794 lines | Thirteen `src/cordis/*.py` files, excluding `src/cordis/loader`. |
| Focused Cordis-py Core tests | 798 lines | Eight existing lifecycle, event, reflect, logger and config test modules. |
| Final Cordis-py Core source | 2,686 lines | Fourteen files after equivalence, still 7 lines below the 2,693-line TypeScript reference. |
| Final observer source | 27 lines | Kept outside Core in `cordis_observer`. |
| Final Python tests | 2,000 lines | Includes Core, Loader and compatibility-profile tests. |

Core grew by 892 lines (50%). Logger completion accounts for roughly one third of the net growth; Context-bound service facades, lifecycle/race handling and portable public contracts account for most of the remainder. The final Core remains slightly smaller than the documented TypeScript source, has one scheduler/store/event bus per capability, and keeps the 27-line learning observer outside Core.

## Paired evidence

| Scenario | TypeScript reference | Python runner | Result |
| --- | --- | --- | --- |
| `004-core-smoke` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output: function-plugin activation, owned listener dispatch, returned cleanup and final `DISPOSED` state. |
| `005-plugin-shapes` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for function, constructor and object-apply activation, cleanup and Registry removal. |
| `006-context-registry` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for extended metadata, missing lookup and Registry inspection. |
| `007-inject-delete` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for dependency activation and fire-and-forget Registry deletion. |
| `008-strict-get` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for strict and loose lookup during provider activation. |
| `009-inject-metadata` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for inherited class Inject metadata and Service method child Fibers across dependency epochs. |
| `010-context-filter` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for Context identity, `baseUrl` inheritance, filtered dispatch, global listeners and dispatch metadata. |
| `011-inject-config` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for Inject object config and service config merge precedence. |
| `012-event-contracts` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for registration replacement, listener disposal and all five dispatch modes, including async listeners. |
| `013-effect-contracts` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for callable/awaitable disposal, iterable cleanup order, invalid results and disposal during async setup. |
| `014-fiber-contracts` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for Fiber fields, restart/update/dispose results and stable transition sequence. |
| `015-fiber-invalid-update` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for active versus pending invalid-update timing and final state. |
| `016-fiber-failures` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized final state for startup rollback/recovery and cleanup failure. |
| `017-fiber-dependency-races` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact deterministic output for service loss during loading and restoration during unloading. |
| `018-reflect-service` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for callable/init/extended services, custom merge, shared isolation, filtered notifications and set results. |
| `019-accessor-mixin` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for accessor decisions, bound mixin methods and owned disposal. |
| `020-logger-contracts` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for levels, named thresholds, structured messages, Fiber association, custom formatting, truncation and buffering. |
| `021-logger-options` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for metadata overrides, default-name hyphenation and deterministic color selection. |
| `022-disposable-list` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for ordered insertion, duplicate identity deletion, per-entry disposers and reverse clearing. |
| `023-service-facades` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for direct context-bound Events, Reflect, Registry and Logger service APIs. |
| `024-remaining-contracts` | Cordis 4.0.2 at the pinned commit | Current Cordis-py | Exact normalized output for awaitable plugin results, dynamic service availability and multi-Fiber Registry deletion. |

## Context and service store

Reference: `context.ts`, plus the Context members declared by `reflect.ts`, `events.ts`, `fiber.ts` and `registry.ts`.

| Public contract | Current status | Gap or evidence required |
| --- | --- | --- |
| `Context()` and built-in services | Exact | Root composition and each built-in service's canonical `ctx` association match in `006-context-registry`. |
| `Context.is(value)` | Language-specific | Python uses `Context.is_context(value)` because `is` is syntax; identity behavior matches in `010-context-filter`. |
| `ctx.extend(meta?)` | Exact | `006-context-registry` covers child metadata inheritance without parent mutation. |
| `ctx.isolate(name, label?)` | Exact | Distinct scopes have lifecycle tests; shared-label resolution matches in `018-reflect-service`. |
| `ctx.intercept(name, config)` | Exact | Default merge order matches in `011`; custom merge integration matches in `018`. |
| `ctx.root` | Exact | Root and derived identity are covered by `006` and `010`. |
| `ctx.baseUrl` | Exact | Original Cordis spelling and child inheritance match in `010-context-filter`. |
| `ctx.events`, `logger`, `reflect`, `registry` | Equivalent | Context-bound facades expose direct canonical calls while sharing one backing service each; behavior matches in `023-service-facades`. |
| Context Symbol keys | Language-specific | `Context.filter` is an opaque Python sentinel; filtered dispatch behavior matches in `010-context-filter`. |
| `ctx.get(name, strict?)` | Exact | Missing values and LOADING-provider strict/loose lookup match in `006` and `008`. |
| `ctx.set(name, value)` | Equivalent | Ownership and success results match; Python also routes accessor decisions through this explicit method. |
| `ctx.provide(name, value, check?)` | Equivalent | Ownership, callable disposal and isolation-filtered notifications match in `018`. |
| `ctx.accessor(name, options)` | Equivalent | Canonical options mapping and read/write/disposal behavior match in `019`; Python callbacks receive the receiver explicitly instead of through JavaScript `this`. |
| `ctx.mixin(name, mixins)` | Exact | Value forwarding, bound methods and disposal match in `019-accessor-mixin`. |

## Registry, Inject and Plugin

Reference: `registry.ts`.

| Public contract | Current status | Gap or evidence required |
| --- | --- | --- |
| `Inject` array/object model | Exact | Array/class behavior matches in `009`; object intercept config matches in `011-inject-config`. |
| `Inject(name, config?)` class decorator | Exact | Inherited class dependencies match in `009-inject-metadata`. |
| `Inject.resolve()` | Exact | Canonical public normalization method reuses the registry's single dependency-normalization path. |
| method-level `Inject` | Equivalent | Service method child-Fiber activation and cleanup match in `009`; Python uses post-construction method scanning instead of decorator initializers. |
| function plugin | Exact | Basic activation/cleanup and identity match in `005-plugin-shapes`. |
| constructor plugin | Equivalent | Construction matches in `005`; lifecycle-owned initialization and cleanup match through the Service constructor scenario in `018`. |
| `{ apply(ctx, config) }` plugin | Equivalent | Activation, identity and cleanup match in `005`; Python reads the same name, Config and inject metadata without prototype mechanics. |
| Plugin `name`, `Config`, `inject`, `provide`, `intercept` | Equivalent | Runtime name, validation and injection metadata are consumed; provide/intercept are structural declarations used by Service and injection configuration rather than a second registry mechanism. |
| Plugin transform `{ schema, Config }` | Language-specific | `schema` only affects TypeScript config inference; callable `Config` validation remains available in Python. |
| `Plugin.Runtime` | Equivalent | Exported `PluginRuntime` exposes name, fibers, callback and Config; `006` verifies the observable fields. Python cannot namespace a runtime class beneath a union type. |
| `ctx.inject(deps, callback)` | Exact | Dependency activation/deletion match in `007`; mapping intercepts match in `011-inject-config`. |
| `ctx.plugin(plugin, config?)` | Equivalent | Returns the Fiber immediately and supports `await fiber`; `024` verifies awaited identity. Python also keeps `fiber.wait()` because `await` cannot be a method name. |
| Registry `counter`, `size`, `resolve` | Exact | Public allocation, size and plugin-shape resolution are covered across `005`, `006` and `014`. |
| Registry `get`, `has`, `delete` | Exact | Identity, return/removal and multi-Fiber disposal match across `006`, `007` and `024`. |
| Registry `keys`, `values`, `entries`, `forEach` | Exact | Portable one-runtime inspection matches in `006-context-registry`. |

## Fiber, Effect and errors

Reference: `fiber.ts`.

| Public contract | Current status | Gap or evidence required |
| --- | --- | --- |
| `ValidationError` | Equivalent | Canonical issue storage and message rendering match in `015`; Python accepts validator callables/objects instead of JavaScript Standard Schema's symbol-named property. |
| `resolveConfig(runtime, config)` | Equivalent | Canonical public helper delegates to the Python validator adapter and is also used internally by Fiber. |
| `Disposable` | Equivalent | `Effect` is callable and awaitable; shared single-shot disposal matches in `013-effect-contracts`. |
| `Effect` accepted result shapes | Equivalent | Callable, iterable and async setup results, reverse cleanup and invalid `TypeError` match in `013`. |
| `EffectMeta` | Exact | Public label/children tree and nested attachment match in `013-effect-contracts`. |
| `FiberState` | Equivalent | All six names and transition behavior match; Python uses string-valued `StrEnum` instead of numeric const-enum values. |
| `CordisError` and `INACTIVE_EFFECT` | Equivalent | `CordisError.Code.INACTIVE_EFFECT`, code value and default message match; additional codes remain classified Python extensions. |
| Fiber `uid`, `ctx`, `_config`, `config`, `inject`, `state` | Exact | Public fields and disposed `uid = None` behavior match in `014`; `None` is Python's `null`. |
| Fiber `dispose`, `store`, `inertia` | Equivalent | Stable and disposed values match in `014`; Python transitions use `asyncio.Task`. |
| Fiber `name`, `assertActive()`, `getEffects()` | Exact | Canonical names exist and redundant pre-equivalence aliases have been removed. |
| `fiber.effect(execute, label?)` | Equivalent | `ctx.effect()` exposes the same callable/awaitable capability through one Python `Effect` object; `013` covers setup/disposal behavior. |
| `fiber.await()` | Language-specific | Python uses `fiber.wait()` because `await` is syntax; stable waiting/error capability is equivalent. |
| `fiber.restart()` | Exact | Cleanup, reactivation, result and transition sequence match in `014`. |
| `fiber.update(config, noSave?)` | Equivalent | Python spells `no_save`; active/pending success and validation timing match in `014`/`015`. |
| dependency epoch unload/reload | Exact | Loading loss and unloading restoration match deterministically in `017-fiber-dependency-races`. |
| setup/dispose races and idempotence | Equivalent | Effect setup/disposal matches in `013`; Fiber cleanup failures reach the same final state in `016`, while Python propagates an `ExceptionGroup`. |

## Events

Reference: `events.ts`.

| Public contract | Current status | Gap or evidence required |
| --- | --- | --- |
| `isBailed(value)` | Exact | Canonical name and `None`/`False` bail semantics match; Python truthiness is intentionally not used. |
| `Parameters`, `ReturnType`, `ThisType` | Language-specific | TypeScript type utilities have no Python runtime contract. |
| `DispatchMode` | Exact | Exported Literal contains the same five mode strings. |
| `EventOptions` and `Hook` | Equivalent | Exported structural types match; Python spells the Hook field `global_` because `global` is a keyword. |
| `emit` | Equivalent | Sync and fire-and-forget async listeners match in `012`; Python schedules awaitables on the running event loop. |
| `parallel` | Equivalent | Awaiting all listeners, aggregate failures and `emit` mode reporting match in `012`. |
| `serial` | Equivalent | Awaited bail behavior matches in `012`. |
| `bail` | Equivalent | Sync bail values and an async listener's immediately returned awaitable match in `012`. |
| `waterfall` | Equivalent | Composition and async results match in `012`; Python exposes an async method. |
| explicit dispatch `thisArg` | Equivalent | Python accepts a dispatch `Context` in the same leading position; filtering matches in `010`. |
| `on`, `once`, prepend and disposer | Exact | Ordering, one-shot removal and idempotent disposer results match in `012`. |
| Context filter and global listener | Equivalent | Opaque-sentinel filter behavior and global bypass match in `010`. |
| `internal/listener` replacement | Exact | Replacement disposer and options match in `012`. |
| `internal/dispatch` | Exact | Arguments and mode reporting match across `010` and `012`. |
| remaining core internal events | Equivalent | Plugin/status/config/service/update timing is covered across `014`-`018`; get/set include the upstream diagnostic error carrier and have focused interception tests. |

## Reflect and Service

Reference: `reflect.ts` and `service.ts`.

| Public contract | Current status | Gap or evidence required |
| --- | --- | --- |
| `Property.Service` / `Property.Accessor` | Equivalent | Exported TypedDict constructors preserve the canonical namespace and fields; callback receiver handling follows the documented Python language difference. |
| `Impl` | Equivalent | Canonical `Impl` exposes name, fiber, value and check; Python additionally stores the isolation label needed by its non-Proxy service map. |
| Reflect `get`, `set`, `provide`, `notify` | Equivalent | Strict lookup, ownership, results and isolation-filtered notifications are covered by `008`, `018` and lifecycle tests. |
| Reflect `accessor`, `mixin`, callback binding | Equivalent | Python callback signatures differ; decisions, method binding and disposal match in `019`. |
| `Service.name` and registration | Exact | Named registration and lifecycle ownership match across `011` and `018`. |
| Service initialization hook | Equivalent | Python `init()` runs after construction and owns its returned cleanup, matching `018`. |
| Service availability check | Equivalent | Python's overridable `available()` replaces the JavaScript symbol method; dependency unload/restoration matches in `024`. |
| callable Service | Equivalent | Python uses `__call__`; consumer-Context invocation matches in `018`. |
| Service extension | Equivalent | `Service.extend(**properties)` creates an unregistered derived service, matching `018`. |
| caller Context tracking | Equivalent | `ServiceView` preserves the calling Context for methods and invocation in `011`/`018`. |
| service config declaration and resolution | Equivalent | Default and custom merge paths match in `011` and `018`. |
| tracker metadata | Language-specific | Preserve association and caller tracing without Symbol identity. |

## Logger

Reference: `logger.ts`.

| Public contract | Current status | Gap or evidence required |
| --- | --- | --- |
| `LoggerType`, `LoggerMethod`, `Formatter` | Equivalent | Python uses callable annotations rather than TypeScript aliases; the runtime method/formatter contracts match in `020`. |
| `LoggerLevel` | Exact | Canonical name and numeric values match in `020-logger-contracts`. |
| `Message` | Exact | Canonical structured fields, serials, args and Fiber association match in `020`. |
| `Exporter` | Equivalent | Python uses a structural Protocol; colors, max length, levels, formatters and export behavior match in `020`. |
| `defaultFormatters` | Exact | All portable placeholders, including color placeholders, are exported and used by `Logger.format()`. |
| `LoggerOptions` | Equivalent | Python accepts the same options mapping; metadata override order and arbitrary exporter-visible fields match in `021`. |
| `Logger` callable level methods | Exact | Canonical `error`, `info`, `warn` and `debug` methods and formatting match in `020`. |
| `c16`, `c256` | Exact | Both upstream palettes are exported unchanged. |
| `LoggerService.Intercept` | Equivalent | Python accepts the same `name`/`level` mapping through `Context.intercept()`. |
| LoggerService callable behavior | Equivalent | `ctx.logger(name)` and context-bound default creation match; Python implements callability with `__call__`. |
| level inheritance, buffer and exporter lifecycle | Exact | Named/default/logger threshold order, bounded buffer, Fiber metadata and owned exporter removal are covered by `020` and focused tests. |

## Public utilities

Reference: `utils.ts`, exported from the package root.

| Public contract | Current status | Gap or evidence required |
| --- | --- | --- |
| `DisposableList` | Exact | Exported ordered weak-identity collection matches in `022-disposable-list`. |
| `Tracker` | Language-specific | Type-only metadata for Proxy/callable behavior. |
| `symbols` | Language-specific | No Symbol identity target; each carried capability is tracked above. |
| `isConstructor`, `isObject` | Language-specific | JavaScript shape tests are covered by Python plugin/result normalization. |
| `joinPrototype`, `getPropertyDescriptor` | Language-specific | Prototype/descriptor mechanics are not portable APIs. |
| `getTraceable`, `withProps`, `createCallable` | Language-specific | Preserve caller Context and callable Service capabilities instead. |
| `composeError`, `buildOuterStack` | Language-specific | These manipulate V8 stack frames; Python preserves causal tracebacks and Effect labels without manufacturing JavaScript stack strings. |

## Final language differences

| Cordis mechanism | Python contract | Reason |
| --- | --- | --- |
| `Context.is()` | `Context.is_context()` | `is` is Python syntax. |
| `Fiber.await()` | `await fiber` or `fiber.wait()` | `await` cannot be a Python method name; the Fiber itself is awaitable. |
| `global` Hook field | `{"global": ...}` remains exact in `EventOptions`; the stored Python Hook uses `global_` | `global` is a Python keyword and cannot be an attribute declaration. |
| Promise/thenable disposal | callable and awaitable `Effect` | Native Python await protocol preserves the capability. |
| Proxy and Symbol tracking | small Context-bound views, sentinels and explicit fields | Python has no equivalent prototype/Symbol identity; service association and caller tracing are preserved. |
| decorator initializers | class/method decorators plus post-construction method scanning | Python decorators do not have JavaScript initializer hooks. |
| Standard Schema symbol | `ConfigValidator` callable/object adapter | Python's validation ecosystem has no shared JavaScript symbol property; issue messages and timing match. |
| JavaScript `AggregateError` | Python `ExceptionGroup` | Native aggregate exception with equivalent cleanup/parallel semantics. |
| V8 stack rewriting | native causal tracebacks plus Effect labels | V8 frame strings are runtime-specific and not portable. |
| `Plugin.Runtime` namespace | exported `PluginRuntime` | Python type unions cannot carry a TypeScript declaration namespace. |

## Cordis-py extensions

These are not evidence of upstream equivalence:

- `Context.aclose()`;
- additional `CordisErrorCode` values;
- `ConfigValidator` and the `PluginContext`, `PluginFunction` and `PluginObject` typing helpers;
- package metadata exposed as `__version__`;
- explicit Loader APIs under `cordis.loader`.

The separate `cordis_observer.MemoryExporter` is development instrumentation, not a Cordis Core extension or package-root API. It reuses the core Exporter and formatter contracts without adding another logger implementation.

Each extension must either remain orthogonal to Cordis behavior or be removed/consolidated when its upstream-equivalent contract is implemented.
