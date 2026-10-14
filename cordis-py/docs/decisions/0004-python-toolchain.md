# ADR 0004: Support Python 3.11 with a uv-based toolchain

Status: Accepted

## Context

Cordis-py needs native asynchronous error aggregation, modern typing, reproducible dependency resolution and a small contributor command surface.

## Decision

- Support Python 3.11 and newer.
- Use Python 3.12 as the default local development interpreter in `.python-version`.
- Use uv for environments, dependency locking, command execution and builds.
- Use hatchling as the PEP 517 build backend.
- Use pytest and pytest-asyncio for tests.
- Use Ruff for linting and formatting.
- Use Pyright in strict mode for static type checking.
- Publish a `src/` layout package with `py.typed`.

## Consequences

- The runtime may use `ExceptionGroup`, `TaskGroup`, `Self` and `tomllib` without compatibility shims.
- Code and type configuration target Python 3.11 even when development runs on 3.12.
- Repository commands are consistently invoked through `uv run`.
- Tool versions are resolved through `uv.lock`.

## Alternatives

- Python 3.12 minimum would simplify some typing but unnecessarily reduce compatibility.
- Poetry or PDM would work but add a second workflow after the repository was initialized with uv.
- Mypy is viable, but Pyright strict mode better matches the planned Protocol-heavy public API.
