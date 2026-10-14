# ADR 0001: Use an async-first lifecycle

Status: Accepted

## Context

Plugin setup, Effect setup, disposer execution, dependent unload and task cancellation may be asynchronous. A synchronous core would either lose completion information or repeatedly create event loops.

## Decision

Make lifecycle completion asynchronous:

- `await fiber.wait()` waits for stable activation state;
- `await fiber.restart()` and `await fiber.update()` wait for transition completion;
- `await fiber.dispose()` and `await ctx.aclose()` are canonical cleanup APIs;
- synchronous plugins, listeners and disposers remain valid;
- synchronous convenience shutdown, if provided, works only outside a running event loop.

## Consequences

- owner cleanup can be fully joined and errors aggregated;
- public examples require an asyncio entry point for complete lifecycle control;
- `Fiber.__await__` remains optional syntactic sugar;
- background tasks must be tracked and cannot be fire-and-forget.

## Alternatives

- Sync-first core with optional async helpers: simpler examples but ambiguous cleanup completion;
- separate sync and async runtimes: duplicates state-machine logic and risks semantic drift.

## Follow-up

Plan 01 must demonstrate that synchronous plugin authoring remains ergonomic while lifecycle completion stays awaitable.
