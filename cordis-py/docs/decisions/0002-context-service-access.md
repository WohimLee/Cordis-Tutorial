# ADR 0002: Provide dynamic and explicit service access

Status: Accepted

## Context

TypeScript Cordis uses a Proxy so `ctx.llm` performs scoped service resolution. Python can use `__getattr__`, but fully dynamic attributes are harder for static typing and may collide with Context members.

## Decision

Provide both forms:

```python
ctx.llm
ctx.get("llm")
```

Dynamic access is ergonomic plugin syntax. Explicit access is the canonical framework-internal API and supports dynamic names. Both call the same ReflectService resolver and enforce identical scope and availability rules.

## Consequences

- plugin examples remain close to Cordis concepts;
- framework code avoids accidental member collisions;
- applications can define typed Context Protocols for known services;
- Context reserves its built-in public member names.

## Alternatives

- explicit `get()` only: easiest to type but less faithful and less ergonomic;
- dynamic attributes only: concise but weak for tooling and internal clarity;
- generated Context subclasses: strong typing but too rigid for dynamic plugins.

## Follow-up

Plan 01 must prototype Pyright behavior, reserve Context member names and define collision errors before freezing the first public release.
