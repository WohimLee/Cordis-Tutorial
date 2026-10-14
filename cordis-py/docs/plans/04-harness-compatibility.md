# Plan 04: DeepSeek Harness Compatibility

Status: Complete

## Goal

Prove the runtime through representative DeepSeek Harness capability composition and document intentional compatibility limits.

## Dependencies

- Plan 03 complete;
- representative Harness services selected from current source.

## In scope

- minimal service-definition/provider/consumer pattern;
- representative llm, tools, sessions and agent-loop seams;
- configuration profile assembled through Loader;
- provider replacement and dependent reload scenarios;
- TypeScript/Python event or transcript comparison;
- compatibility matrix and plugin-author tutorial.

## Out of scope

- immediate port of every Harness package;
- byte-for-byte logs or JavaScript object representation;
- matching implementation-private TypeScript APIs.

## Work packages

- [x] Inventory runtime APIs used by selected Harness packages.
- [x] Define Python service protocols for representative seams.
- [x] Implement test providers and consumers.
- [x] Assemble a runnable profile without manual boot order.
- [x] Record provider replacement and shutdown sequences.
- [x] Compare observable behavior with TypeScript fixtures.
- [x] Publish compatibility matrix and intentional differences.

## Acceptance criteria

- Representative application starts from configuration.
- Provider replacement unloads and reloads the expected dependency subtree.
- Session/tool/agent interactions use services and events rather than concrete imports.
- Shutdown leaves no tracked Effect, task, service or listener.
- A contributor can implement and configure a new plugin from project docs alone.

## Verification

- runnable example profile;
- keyless integration transcript;
- leak-free shutdown assertion;
- compatibility scenario suite;
- full project checks.

## Completion evidence

- A keyless YAML profile executes LLM/tools/sessions/agent-loop composition.
- JSON scenarios record adapter HMR, service-loss cascade and leak-free shutdown.
- Harness scenarios name their vendored TypeScript declaration or test evidence.
- The compatibility matrix and provider tutorial document supported seams and limits.
- Ruff, Ruff format, Pyright strict, pytest and package build pass.
