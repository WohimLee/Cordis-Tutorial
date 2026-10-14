# ADR 0005: Target Cordis Core equivalence

Status: Accepted

Supersedes: [ADR 0003](0003-behavioral-compatibility.md)

## Context

ADR 0003 selected behavioral compatibility while allowing intentional public API differences. The implemented runtime now covers the main lifecycle model, but that policy is too loose for the project's next objective.

The compatibility reference is `vendor/cordis` from the vendored DeepSeek Harness repository, currently package version 4.0.2. Harness capability packages and the separate Loader, Include, HMR and Timer packages are not part of the Cordis Core equivalence boundary.

## Decision

Cordis-py targets Cordis Core equivalence in three dimensions:

1. concepts use the same names and relationships;
2. public APIs use the same names, arguments, return contracts and errors where Python can express them;
3. the same public operation sequence produces the same observable lifecycle result.

JavaScript implementation mechanisms are not requirements by themselves. Proxy, Symbol, prototype manipulation, declaration merging, thenables and explicit `this` are replaced only when Python cannot express them directly. Each replacement must preserve the public capability and be recorded in a language-difference table with tests.

Python conventions do not justify an API difference when the Cordis name is legal and practical in Python. Python-keyword conflicts and runtime-model constraints are explicit exceptions, not permission to redesign the API generally.

Compatibility is established by an inventory generated or checked against the vendored TypeScript source and by paired TypeScript/Python behavior scenarios. Architecture examples and existing Python behavior are not evidence that an upstream API has been reproduced.

Implementation remains compact:

- one scheduler owns dependency activation;
- one Effect implementation handles synchronous and asynchronous results;
- compatibility entry points delegate to the same state and logic;
- JavaScript-only machinery is not simulated with parallel abstraction layers;
- every new compatibility abstraction must close a named matrix gap.

## Consequences

- Existing Python APIs may be renamed or narrowed before the package reaches a stable release.
- Thin aliases are temporary migration tools, not a second permanent API.
- `fiber.await()` and keyword arguments such as `global` require documented Python spellings.
- Extra Python behavior is allowed only when it does not change the corresponding Cordis contract and is labeled as an extension.
- Logger is part of Cordis Core and must eventually match its public capability, even though it also supports runtime learning and diagnostics.
- Plan 05 audits the current implementation before changing behavior and removes obsolete abstractions while closing gaps.
