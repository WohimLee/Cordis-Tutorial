# ADR 0003: Target behavioral compatibility

Status: Superseded by [ADR 0005](0005-cordis-core-equivalence.md)

## Context

Vendored Cordis relies on TypeScript and JavaScript features including Proxy, Symbol, prototype inheritance, declaration merging and thenable objects. Mechanical translation would produce unnatural Python without improving observable compatibility.

## Decision

Reproduce public behavior and lifecycle event sequences rather than language-specific implementation mechanics.

Compatibility assertions focus on:

- service visibility and isolation;
- Fiber state transitions;
- dependency-driven unload/reload;
- Effect ownership and cleanup;
- event listener order and results;
- configuration update and failure outcomes.

Python uses idiomatic equivalents such as `__getattr__`, Protocol, private sentinels and explicit await methods.

## Consequences

- TypeScript source remains the behavior reference;
- cross-language tests compare scenarios, not object layouts;
- intentional API differences are documented in a compatibility matrix;
- JavaScript-only implementation details are not porting requirements.
